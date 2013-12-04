# coding: utf-8
import os
import os.path
import json
import re
import traceback
import atexit
import subprocess
import webbrowser
import imp
from functools import wraps

try:
    unicode()
except NameError:
    unicode = str

try:
    import urllib
    urllib = imp.reload(urllib)
    from urllib import request
    request = imp.reload(request)
    Request = request.Request
    urlopen = request.urlopen
    HTTPError = urllib.error.HTTPError
    URLError = urllib.error.URLError
    assert Request and urlopen and HTTPError and URLError
except ImportError:
    import urllib2
    urllib2 = imp.reload(urllib2)
    Request = urllib2.Request
    urlopen = urllib2.urlopen
    HTTPError = urllib2.HTTPError
    URLError = urllib2.URLError

import vim

try:
    from floo.common import api, ignore, migrations, msg, reactor, shared as G, utils
    from floo.common.handlers.account import CreateAccountHandler
    from floo.common.handlers.credentials import RequestCredentialsHandler
    from floo.vim_handler import VimHandler
    from floo import editor
except (ImportError, ValueError):
    from floo.common import api, ignore, migrations, msg, reactor, shared as G, utils
    from floo.common.handlers.account import CreateAccountHandler
    from floo.common.handlers.credentials import RequestCredentialsHandler
    from floo.vim_handler import VimHandler
    from floo import editor


reactor = reactor.reactor

G.__VERSION__ = '0.10'
G.__PLUGIN_VERSION__ = '1.0.0'

utils.reload_settings()

# enable debug with let floo_log_level = 'debug'
floo_log_level = vim.eval('floo_log_level')
msg.LOG_LEVEL = msg.LOG_LEVELS.get(floo_log_level.upper(), msg.LOG_LEVELS['MSG'])

migrations.rename_floobits_dir()
migrations.migrate_symlinks()

G.DELETE_LOCAL_FILES = bool(int(vim.eval('floo_delete_local_files')))
G.SHOW_HIGHLIGHTS = bool(int(vim.eval('floo_show_highlights')))
G.SPARSE_MODE = bool(int(vim.eval('floo_sparse_mode')))
G.TIMERS = bool(int(vim.eval('has("timers")')))


call_feedkeys = False
ticker = None
ticker_errors = 0
using_feedkeys = False

ticker_python = """import sys; import subprocess; import time
args = ['{binary}', '--servername', '{servername}', '--remote-expr', 'g:floobits_global_tick()']
while True:
    time.sleep({sleep})
    # TODO: learn to speak vim or something :(
    proc = subprocess.Popen(args,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE)
    (stdoutdata, stderrdata) = proc.communicate()
    # # yes, this is stupid...
    if stdoutdata.strip() == '0':
        continue
    if len(stderrdata) == 0:
        continue
    sys.stderr.write(stderrdata)
    sys.exit(1)
"""

FLOOBITS_INFO = """
floobits_version: {version}
# not updated until FlooJoinWorkspace is called
mode: {mode}
updatetime: {updatetime}
clientserver_support: {cs}
servername: {servername}
ticker_errors: {ticker_errors}
"""


def floo_info():
    kwargs = {
        'cs': bool(int(vim.eval('has("clientserver")'))),
        'mode': (using_feedkeys and 'feedkeys') or 'client-server',
        'servername': vim.eval("v:servername"),
        'ticker_errors': ticker_errors,
        'updatetime': vim.eval('&l:updatetime'),
        'version': G.__PLUGIN_VERSION__,
    }

    msg.log(FLOOBITS_INFO.format(**kwargs))


def floo_pause():
    global call_feedkeys, ticker

    if G.TIMERS:
        return

    if using_feedkeys:
        call_feedkeys = False
        vim.command("set updatetime=4000")
    else:
        if ticker is None:
            return
        try:
            ticker.kill()
        except Exception as e:
            print(e)
        ticker = None


def floo_unpause():
    global call_feedkeys

    if G.TIMERS:
        return

    if using_feedkeys:
        call_feedkeys = True
        vim.command("set updatetime=250")
    else:
        start_event_loop()


def fallback_to_feedkeys(warning):
    global using_feedkeys
    using_feedkeys = True
    warning += " Falling back to f//e hack which will break some key commands. You may need to call FlooPause/FlooUnPause before some commands."
    msg.warn(warning)
    floo_unpause()


def ticker_watcher(ticker):
    global ticker_errors
    if not G.AGENT:
        return
    ticker.poll()
    if ticker.returncode is None:
        return
    msg.warn('respawning new ticker')
    ticker_errors += 1
    if ticker_errors > 10:
        return fallback_to_feedkeys('Too much trouble with the floobits external ticker.')
    start_event_loop()
    utils.set_timeout(ticker_watcher, 2000, ticker)


def start_event_loop():
    global ticker

    if G.TIMERS:
        msg.debug('Your Vim was compiled with +timer support. Awesome!')
        return

    if not bool(int(vim.eval('has("clientserver")'))):
        return fallback_to_feedkeys("This VIM was not compiled with clientserver support. You should consider using a different vim!")

    exe = getattr(G, 'VIM_EXECUTABLE', None)
    if not exe:
        return fallback_to_feedkeys("Your vim was compiled with clientserver, but I don't know the name of the vim executable.  Please define it in your ~/.floorc using the vim_executable directive. e.g. 'vim_executable mvim'.")

    servername = vim.eval("v:servername")
    if not servername:
        return fallback_to_feedkeys('I can not identify the servername of this vim. You may need to pass --servername to vim at startup.')

    evaler = ticker_python.format(binary=exe, servername=servername, sleep='1.0')
    ticker = subprocess.Popen(['python', '-c', evaler],
                              stderr=subprocess.PIPE,
                              stdout=subprocess.PIPE)
    ticker.poll()
    utils.set_timeout(ticker_watcher, 500, ticker)


def vim_choice(prompt, default, choices):
    default = choices.index(default) + 1
    choices_str = '\n'.join(['&%s' % choice for choice in choices])
    try:
        choice = int(vim.eval('confirm("%s", "%s", %s)' % (prompt, choices_str, default)))
    except KeyboardInterrupt:
        return None
    if choice == 0:
        return None
    return choices[choice - 1]


def vim_input(prompt, default, completion=None):
    vim.command('call inputsave()')
    if completion:
        cmd = "let user_input = input('%s', '%s', '%s')" % (prompt, default, completion)
    else:
        cmd = "let user_input = input('%s', '%s')" % (prompt, default)
    vim.command(cmd)
    vim.command('call inputrestore()')
    return vim.eval('user_input')


def global_tick():
    reactor.tick()


def cursor_hold():
    global_tick()
    if not call_feedkeys:
        return
    return vim.command("call feedkeys(\"f\\e\", 'n')")


def cursor_holdi():
    global_tick()
    if not call_feedkeys:
        return
    linelen = int(vim.eval("col('$')-1"))
    if linelen > 0:
        if int(vim.eval("col('.')")) == 1:
            vim.command("call feedkeys(\"\<Right>\<Left>\",'n')")
        else:
            vim.command("call feedkeys(\"\<Left>\<Right>\",'n')")
    else:
        vim.command("call feedkeys(\"\ei\",'n')")


def is_connected(warn=False):
    def outer(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if reactor.is_ready():
                return func(*args, **kwargs)
            if warn:
                msg.error('ignoring request (%s) because you aren\'t in a workspace.' % func.__name__)
            else:
                msg.debug('ignoring request (%s) because you aren\'t in a workspace.' % func.__name__)
        return wrapped
    return outer


@is_connected()
def maybe_selection_changed(ping=False):
    G.AGENT.maybe_selection_changed(vim.current.buffer, ping)


@is_connected()
def maybe_buffer_changed():
    G.AGENT.maybe_buffer_changed(vim.current.buffer)


@is_connected()
def follow(follow_mode=None):
    if follow_mode is None:
        follow_mode = not G.STALKER_MODE
    G.STALKER_MODE = follow_mode


@is_connected()
def maybe_new_file():
    path = vim.current.buffer.name
    if path is None or path == "":
        msg.debug('get:buf buffer has no filename')
        return None

    if not os.path.exists(path):
        return None
    if not utils.is_shared(path):
        msg.debug('get_buf: %s is not shared' % path)
        return None

    buf = G.AGENT.get_buf_by_path(path)
    if not buf:
        if not ignore.is_ignored(path):
            G.AGENT.upload(path)


@is_connected()
def on_save():
    buf = G.AGENT.get_buf_by_path(vim.current.buffer.name)
    if buf:
        G.AGENT.send({
            'name': 'saved',
            'id': buf['id'],
        })


@is_connected(True)
def open_in_browser():
    url = G.AGENT.workspace_url
    if 'kick' in G.PERMS:
        url += '/settings'
    else:
        url += '/info'
    webbrowser.open(url)


@is_connected(True)
def add_buf(path=None):
    path = path or vim.current.buffer.name
    G.AGENT._upload(path)


@is_connected(True)
def delete_buf():
    name = vim.current.buffer.name
    G.AGENT.delete_buf(name)


@is_connected()
def buf_enter():
    buf = G.AGENT.get_buf_by_path(vim.current.buffer.name)
    if not buf:
        return
    # NOTE: we call highlight twice in follow mode... thats stupid
    for user_id, highlight in G.AGENT.user_highlights.items():
        if highlight['id'] == buf['id']:
            G.AGENT._on_highlight(highlight)


@is_connected()
def floo_clear():
    buf = G.AGENT.get_buf_by_path(vim.current.buffer.name)
    if not buf:
        return
    view = G.AGENT.get_view(buf['id'])
    if view:
        for user_id, username in G.AGENT.workspace_info['users'].items():
            view.clear_highlight(int(user_id))


@is_connected()
def floo_toggle_highlights():
    G.SHOW_HIGHLIGHTS = not G.SHOW_HIGHLIGHTS
    if G.SHOW_HIGHLIGHTS:
        buf_enter()
        msg.log('Highlights enabled')
        return
    floo_clear()
    msg.log('Highlights disabled')


def share_dir_private(dir_to_share):
    return share_dir(dir_to_share, perms={'AnonymousUser': []})


def share_dir(dir_to_share, perms=None):
    dir_to_share = os.path.expanduser(dir_to_share)
    dir_to_share = utils.unfuck_path(dir_to_share)
    dir_to_share = os.path.abspath(dir_to_share)
    dir_to_share = os.path.realpath(dir_to_share)

    workspace_name = os.path.basename(dir_to_share)

    if os.path.isfile(dir_to_share):
        return msg.error('give me a directory please')

    if not os.path.isdir(dir_to_share):
        return msg.error('The directory %s doesn\'t appear to exist' % dir_to_share)

    floo_file = os.path.join(dir_to_share, '.floo')
    # look for the .floo file for hints about previous behavior
    info = {}
    try:
        floo_info = open(floo_file, 'rb').read().decode('utf-8')
        info = json.loads(floo_info)
    except (IOError, OSError):
        pass
    except Exception:
        msg.warn("couldn't read the floo_info file: %s" % floo_file)

    workspace_url = info.get('url')
    if workspace_url:
        try:
            result = utils.parse_url(workspace_url)
        except Exception as e:
            msg.error(str(e))
        else:
            workspace_name = result['workspace']
            try:
                # TODO: blocking. beachballs sublime 2 if API is super slow
                api.get_workspace_by_url(workspace_url)
            except HTTPError:
                workspace_url = None
                workspace_name = os.path.basename(dir_to_share)
            else:
                utils.add_workspace_to_persistent_json(result['owner'], result['workspace'], workspace_url, dir_to_share)

    workspace_url = utils.get_workspace_by_path(dir_to_share) or workspace_url

    if workspace_url:
        try:
            api.get_workspace_by_url(workspace_url)
        except HTTPError:
            pass
        else:
            return join_workspace(workspace_url, dir_to_share, sync_to_disk=False)

    orgs = api.get_orgs_can_admin()
    orgs = json.loads(orgs.read().decode('utf-8'))
    if len(orgs) == 0:
        return create_workspace(workspace_name, dir_to_share, G.USERNAME, perms)
    choices = []
    choices.append(G.USERNAME)
    for o in orgs:
        choices.append(o['name'])

    owner = vim_choice('Create workspace for:', G.USERNAME, choices)
    if owner:
        create_workspace(workspace_name, dir_to_share, owner, perms)


def create_workspace(workspace_name, share_path, owner, perms=None):
    try:
        api_args = {
            'name': workspace_name,
            'owner': owner,
        }
        if perms:
            api_args['perms'] = perms
        api.create_workspace(api_args)
        workspace_url = 'https://%s/%s/%s' % (G.DEFAULT_HOST, G.USERNAME, workspace_name)
        msg.debug('Created workspace %s' % workspace_url)
    except HTTPError as e:
        err_body = e.read()
        msg.error('Unable to create workspace: %s %s' % (unicode(e), err_body))
        if e.code not in [400, 402, 409]:
            return editor.error_message('Unable to create workspace: %s %s' % (unicode(e), err_body))

        if e.code == 400:
            workspace_name = re.sub('[^A-Za-z0-9_\-]', '-', workspace_name)
            workspace_name = vim_input('Invalid name. Workspace names must match the regex [A-Za-z0-9_\-]. Choose another name:' % workspace_name, workspace_name)
        elif e.code == 402:
            # TODO: better behavior. ask to create a public workspace instead
            return editor.error_message('Unable to create workspace: %s %s' % (unicode(e), err_body))
        elif e.code == 409:
            workspace_name = vim_input('Workspace %s already exists. Choose another name: ' % workspace_name, workspace_name + "1")
        return create_workspace(workspace_name, share_path, perms)
    except Exception as e:
        editor.error_message('Unable to create workspace: %s' % str(e))
        return

    join_workspace(workspace_url, share_path, sync_to_disk=False)


def stop_everything():
    if G.AGENT:
        reactor.stop()
        G.AGENT = None
    floo_pause()
    #TODO: get this value from vim and reset it
    vim.command("set updatetime=4000")

#NOTE: not strictly necessary
atexit.register(stop_everything)


def has_browser():
    valid_browsers = [
        "MacOSX", #Default mac browser.
        "Chrome",
        "Chromium",
        "Firefox",
        "Safari",
        "Opera"
    ]
    for browser in valid_browsers:
        try:
            webbrowser.get(browser)
            return True
        except Exception as e:
            continue
    return False


def complete_signup():
    msg.debug("Completing signup.")
    if not has_browser():
        msg.log("You need a modern browser to complete the sign up. Go to https://floobits.com to sign up.")
        return
    floorc = utils.load_floorc()
    username = floorc.get('USERNAME')
    secret = floorc.get('SECRET')
    msg.debug("Completing sign up with %s %s" % (username, secret))
    if not (username and secret):
        return msg.error('You don\'t seem to have a Floobits account of any sort.')
    webbrowser.open('https://%s/%s/pinocchio/%s/' % (G.DEFAULT_HOST, username, secret))


def check_credentials():
    if not has_browser():
        msg.log("You need a Floobits account to use the Floobits plugin. Go to https://floobits.com to sign up.")
        return
    msg.debug("Print checking credentials.")
    if not (G.USERNAME and G.SECRET):
        setup_credentials()

def setup_credentials():
    prompt = "You need a Floobits account! Do you have one? If no we will create one for you [y/n]. "
    d = vim_input(prompt, "")
    if d and (d != "y" and d != "n"):
        return setup_credentials()
    agent = None
    if d == "y":
        msg.debug("You have an account.")
    elif not utils.get_persistent_data().get('disable_account_creation'):
        agent = CreateAccountHandler()
    if not agent:
        msg.error("A configuration error occured earlier. Please go to floobits.com and sign up to use this plugin.\n\nWe\'re really sorry. This should never happen.")
        return
    try:
        reactor.connect(agent, G.DEFAULT_HOST, G.DEFAULT_PORT, True)
    except Exception as e:
        msg(str(e))
        msg.log(traceback.format_exc())


def join_workspace(workspace_url, d='', sync_to_disk=True):
    msg.debug("workspace url is %s" % workspace_url)

    try:
        result = utils.parse_url(workspace_url)
    except Exception as e:
        return msg.error(str(e))

    if d:
        utils.mkdir(d)
    else:
        try:
            d = utils.get_persistent_data()['workspaces'][result['owner']][result['workspace']]['path']
        except Exception:
            d = os.path.realpath(os.path.join(G.COLAB_DIR, result['owner'], result['workspace']))

    prompt = "Give me a directory to sync data to: "
    if not os.path.isdir(d):
        while True:
            d = vim_input(prompt, d, "dir")
            if d == '':
                continue
            d = os.path.realpath(os.path.expanduser(d))
            if os.path.isfile(d):
                prompt = '%s is not a directory. Enter an existing path or a path I can create: ' % d
                continue
            if not os.path.isdir(d):
                try:
                    utils.mkdir(d)
                except Exception as e:
                    prompt = "Couldn't make dir: %s because %s " % (d, str(e))
                    continue
            break
    d = os.path.realpath(os.path.abspath(d) + os.sep)
    try:
        utils.add_workspace_to_persistent_json(result['owner'], result['workspace'], workspace_url, d)
    except Exception as e:
        return msg.error("Error adding workspace to persistent.json: %s" % str(e))

    G.PROJECT_PATH = d
    vim.command('cd %s' % G.PROJECT_PATH)
    msg.debug("Joining workspace %s" % workspace_url)

    stop_everything()
    try:
        conn = VimHandler(result['owner'], result['workspace'])
        reactor.connect(conn, result['host'], result['port'], result['secure'])
        if not sync_to_disk:
            conn.once('room_info', lambda agent: agent.upload(G.PROJECT_PATH))
    except Exception as e:
        msg.error(str(e))
        tb = traceback.format_exc()
        msg.debug(tb)
    if not G.TIMERS:
        start_event_loop()


def part_workspace():
    if not G.AGENT:
        return msg.warn('Unable to leave workspace: You are not joined to a workspace.')
    stop_everything()
    msg.log('You left the workspace.')
