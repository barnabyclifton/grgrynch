"""
  paver.ext.gae
  ~~~~~~~~~~~~~

  paver extension for google app engine.


  :copyright: (c) 2014 by gregorynicholas.
  :license: MIT, see LICENSE for more details.
"""
from __future__ import unicode_literals
from paver.easy import Bunch, options as opts, cmdopts
from paver.easy import task, call_task
from paver.easy import BuildFailure, error, path
from paver.ext import archives, http, supervisor, pip
from paver.ext import release, utils
from paver.ext.utils import rm, sh
from paver.ext.gae import remote_api
from jinja2 import Environment
from jinja2.loaders import DictLoader

from paver.ext.gae.appcfg import appcfg
from paver.ext.gae.backends import *
from paver.ext.gae.cron import *
from paver.ext.gae.dos import *
from paver.ext.gae.index import *
from paver.ext.gae.queue import *


__all__ = [
  "appcfg", "install_runtime_libs", "build_descriptors", "fix_gae_sdk_path",
  "verify_serving", "ServerStartFailure",
  "install_sdk", "install_mapreduce_lib",
  "datastore_init", "open_admin",
  "server_run", "server_stop", "server_restart", "server_tail",
  "deploy", "deploy_branch", "update_indexes",
  "backends", "backends_rollback", "backends_deploy", "get_backends",
]


opts(
  gae=Bunch(
    sdk=Bunch(
      root=opts.proj.dirs.venv / opts.proj.dev_appserver.ver,
    ),
    dev_appserver=opts.proj.dev_appserver
  ))


class ServerStartFailure(BuildFailure):
  """
  """


class SdkServerNotRunningFailure(BuildFailure):
  """
  """


def _load_descriptors():
  """
    :returns: instance of a `jinja2.Environment`
  """
  descriptors = opts.proj.dirs.gae.descriptors.walkfiles("*.yaml")
  rv = Environment(loader=DictLoader(
    {str(d.name): str(d.text()) for d in descriptors}
  ))
  return rv


def build_descriptors(dest, env_id, ver_id=None):
  """
    :param dest:
    :param env_id:
    :param ver_id:
  """
  if ver_id is None:
    ver_id = release.dist_version_id()

  context = dict(
    app_id=opts.proj.app_id,
    env_id=env_id,
    ver_id=ver_id,
    templates_dir=str(opts.proj.dirs.app_web_templates.relpath()),
    static_dir=str(opts.proj.dirs.app_web_static.relpath()))

  _render_jinjaenv(_load_descriptors(), context, dest)


def _create_descriptor(name, template, context, dest):
  """
  parses the config template files and generates yaml descriptor files in
  the root directory.

    :param template: instance of a jinja2 template object
    :param context: instance of a dict
    :param dest: instance of a paver.easy.path object
  """
  descriptor = dest / name.replace(".template", "")
  descriptor.write_text(template.render(**context))


def _render_jinjaenv(jinjaenv, context, dest):
  """
  """
  for name, _ in jinjaenv.loader.mapping.iteritems():
    _create_descriptor(
      name.replace(".template", ""),
      jinjaenv.get_template(name), context, dest)


def verify_serving(url, retries=15, sleep=2):
  """
  pings a url. used as a health check.

    :param url:
    :param retries:
    :param sleep:
  """
  try:
    http.ping(url, retries=retries, sleep=sleep)
  except http.PingError:
    raise SdkServerNotRunningFailure("""
  can't connect to local appengine sdk server..
  """)
  else:
    print "connected to local appengine server.."


def fix_gae_sdk_path():
  """
  hack to load the app engine sdk into the python path.
  """
  import dev_appserver
  dev_appserver.fix_sys_path()


def _dev_appserver_config(config_id="default"):
  """
  returns config dict for the dev_appserver of a specified environ.
  """
  config = opts.gae.dev_appserver[config_id]
  if "blobstore_path" not in config["args"]:
    config["args"]["blobstore_path"] = opts.proj.dirs.data.blobstore

  if "datastore_path" not in config["args"]:
    config["args"]["datastore_path"] = opts.proj.dirs.data.datastore_file

  return config


def parse_flags(cfg):
  """
  returns a string to run as the `dev_appserver.py` command.
  """
  flags = utils.parse_cmd_flags(cfg["args"], cfg["flags"])
  return "{}/dev_appserver.py {} .".format(opts.gae.sdk.root, flags)


def install_runtime_libs(packages, dest):
  """
  since app engine doesn't allow for fucking pip installs, we have to symlink
  the libs to a local project directory. we could do 2 separate pip installs,
  but that shit gets slow as fuck.

    :todo: zip each lib inside of the lib dir to serve third party libs
  """
  for f in pip.get_installed_top_level_files(packages):
    print "sym linking: ", f
    f.sym(dest / f.name)
    # todo: check for missing `__init__.py`
    # check = dest / f.name / '__init__.py'
    # if not check.exists():


@task
def install_sdk():
  """
  installs the app engine sdk to the virtualenv.
  """
  # todo: add a "force" option to override cache
  archive = path(opts.gae.dev_appserver.ver + ".zip")
  if not (opts.proj.dirs.venv / archive).exists():
    http.wget(
      opts.gae.dev_appserver.src + archive.name, opts.proj.dirs.venv)

  rm(opts.gae.sdk.root)
  sh(
    """
    unzip -d ./ -oq {archive} && mv ./google_appengine {sdk_root}
    """,
    archive=archive.name,
    sdk_root=opts.gae.sdk.root,
    cwd=opts.proj.dirs.venv,
    err=True)

  if not opts.gae.sdk.root.exists():
    raise BuildFailure("shit didn't download + extract the lib properly.")

  # integrate the app engine sdk with virtualenv
  pth_path = opts.proj.dirs.venv / "lib/python2.7/site-packages/gaesdk.pth"
  pth_path.write_lines([
    opts.gae.sdk.root.abspath(),
    "import dev_appserver",
    "dev_appserver.fix_sys_path()"])

  # use virtualenv hooks to add gae's sdk to the exec path
  postactivate = opts.proj.dirs.venv / "bin/postactivate"
  postactivate.write_lines([
    "#!/usr/bin/env bash",
    "echo \"exec path adjusted for google_appengine_1.8.9\"",
    "export PATH=\"{}\":$PATH".format(opts.gae.sdk.root)])


@task
def install_mapreduce_lib():
  """
  installs google app engine's mapreduce library.
  (http://github.com/gregorynicholas/appengine-mapreduce)
  """
  if (opts.proj.dirs.lib / "mapreduce.zip").exists():
    return
  print "installing app engine mapreduce libraries.."

  rm(opts.proj.dirs.lib / "appengine-mapreduce-master")
  rm(opts.proj.dirs.lib / "mapreduce")
  rm(opts.proj.dirs.lib / "appengine_pipeline")

  if not (opts.proj.dirs.lib / "master.tar.gz").exists():
    http.wget("https://github.com/gregorynicholas/appengine-mapreduce/"
              "archive/master.tar.gz", opts.proj.dirs.lib)

  archives.extract("master.tar.gz", opts.proj.dirs.lib)

  (opts.proj.dirs.lib / "appengine-mapreduce-master/mapreduce"
   ).move(opts.proj.dirs.lib)

  (opts.proj.dirs.lib / "appengine-mapreduce-master/appengine_pipeline"
   ).move(opts.proj.dirs.lib)

  rm(opts.proj.dirs.lib / "appengine-mapreduce-master")


@task
@cmdopts([
  ("set-default", "d", "set the current dist version as the default serving "
                       "version on app engine.", False),
  ("clear-cookies=", "c", "clear the local cookiejar before deploying.", True),
  ("deploy-backends", "b", "flag to deploy the backend servers.", False),
])
def deploy(options):
  """
  deploys the app to google app engine production servers.
  """
  ver_id = release.dist_version_id()
  appcfg(
    "update -v ",
    error=False, capture=True, cwd=opts.proj.dirs.dist)

  # if options.set_default:
  #   set_default_serving_version(ver_id)

  # if options.deploy_backends:
  #   call_task("backends_deploy")

  print("---> deploy success\n")


def set_default_serving_version(ver_id):
  """
  sets the default serving version on app engine production servers.

    :param ver_id:
  """
  appcfg(
    "set_default_version",
    version=ver_id,
    cwd=opts.proj.dirs.dist)


@task
def deploy_branch(options):
  """
  deploy current branch to named instance onto the integration environment.
  """
  call_task("build")

  _opts = opts.Namespace()
  _opts.env_id = opts.proj.envs.integration
  call_task("dist", options=_opts)

  _opts = opts.Namespace()
  _opts.default = False
  call_task("deploy", options=_opts)


@task
def update_indexes():
  """
  updates model index definitions on google app engine production servers.
  """
  appcfg("vacuum_indexes", quiet=True, cwd=opts.proj.dirs.dist)
  appcfg("update_indexes", quiet=True, cwd=opts.proj.dirs.dist)
  print("---> update_indexes success\n")


@task
def datastore_init():
  """
  cleans + creates the local app engine sdk server blobstore & datastore.
  """
  rm(opts.proj.dirs.data.datastore,
     opts.proj.dirs.data.blobstore)
  opts.proj.dirs.data.blobstore.makedirs()
  opts.proj.dirs.data.datastore.makedirs()
  opts.proj.dirs.data.datastore_file.touch()
  print("---> datastore_init success\n")


dev_appserver_config_opt = (
  "config_id=", "c", "name of the configuration profile, defined in "
                     "dev_appserver.yaml, to run the server with.")


@task
@cmdopts([dev_appserver_config_opt])
def server_run(options):
  """
  starts a google app engine server for local development.
  """
  env_id = options.get("env_id", opts.proj.envs.local)
  ver_id = release.dist_version_id()
  config_id = options.get("config_id", "default")
  # pid = opts.proj.dirs.gae.dev_appserver_pid
  # if supervisor.is_pid_running(pid):
  #   raise ServerStartFailure("app engine sdk server is already running..")

  dev_appserver_command = parse_flags(_dev_appserver_config(config_id))

  # generate the supervisord.conf file
  context = dict(
    dev_appserver_command=dev_appserver_command,
    app_id=opts.proj.app_id,
    env_id=env_id,
    ver_id=ver_id)
  template = opts.proj.dirs.buildconfig / 'supervisord.template.conf'

  jinjaenv = Environment(loader=DictLoader(
    {'supervisord.template.conf': str(template.text())}
  ))

  _render_jinjaenv(jinjaenv, context, opts.proj.dirs.base)
  supervisor.start()

  stdout = supervisor.run('devappserver-{}'.format(env_id))
  if "ERROR (already started)" in stdout:
    raise ServerStartFailure("server already running..")


@task
@cmdopts([dev_appserver_config_opt])
def server_stop(options):
  """
  stops the google app engine sdk server.
  """
  env_id = options.get("env_id", opts.proj.envs.local)
  supervisor.stop('devappserver-{}'.format(env_id))
  supervisor.shutdown()
  killall()  # remove any leaks..


@task
@cmdopts([
  ("config_id=", "c", "name of the configuration profile, defined in "
                      "project.yaml, to run the server with.")
])
def server_restart(options):
  """
  restarts the local Google App Engine SDK server.
  """
  server_stop(options)
  server_run(options)


@task
def server_tail():
  """
  view the dev_appserver logs by running the unix native "tail" command.
  """
  stdout = path("{}.out".format(opts.proj.dirs.gae.dev_appserver_pid.name))
  sh("tail -f {pid}", pid=stdout)


@task
@cmdopts([
  ("config_id=", "c", "name of the configuration profile, defined in "
                      "project.yaml, to run the server with.")
])
def open_admin(options):
  """
  opens the google app engine sdk admin console.
  """
  config_id = options.get("config_id", "default")
  sh("open http://{url}:{port}/",
     cwd=opts.proj.dirs.base,
     **_dev_appserver_config(config_id).get("args"))


@task
def create_oauth2_token():
  """
  create an oauth2 token to use for subsequent calls to google.
  """
  run = "appcfg.py update --skip_sdk_update_check --oauth2 "
  " --noauth_local_webserver . "
  sh(run)


@task
def refresh_oauth2_token():
  """
  create an oauth2 token to use for subsequent calls to google.
  """
  run = "appcfg.py update --skip_sdk_update_check "
  " --oauth2_refresh_token {} . "
  sh(run)


def remote(options):
  """
  attaches to an app engine remote_api endpoint.
  """
  env_id = options.get("env_id", opts.proj.envs.local)

  dev_appserver = opts.proj.dev_appserver[env_id]
  host = "{}:{}".format(dev_appserver.host, dev_appserver.port)

  partition = options.get("partition", dev_appserver.partition)
  app_name = options.get("app_name", remote_api.DEFAULT_APP_NAME)
  host = options.get("host", remote_api.DEFAULT_HOST_NAME)
  path = options.get("path", remote_api.DEFAULT_ENDPOINT_PATH)
  email = options.get("email")
  password = options.get("password")

  if env_id == opts.proj.envs.local:
    verify_serving(host)
    partition = "dev"

  if email is None and host != remote_api.DEFAULT_HOST_NAME:
    email = opts.proj.email
    password = opts.proj.password

  fix_gae_sdk_path()
  print "connecting to remote_api: ", env_id, host, email, password
  remote_api.connect(
    "{}~{}-{}".format(partition, app_name, env_id),
    path=path, host=host, email=email, password=password)


@task
def killall():
  """
  quietly kills all known processes known to god and man.
  """
  try:
    sh("killall node", capture=True)
    sh("killall dev_appserver.py", capture=True)
    sh("killall _python_runtime.py", capture=True)
    # sh("killall python", capture=True)
  except:
    pass