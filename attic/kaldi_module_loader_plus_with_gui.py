
"""
original script: https://github.com/caspark/dragonfly-frons

This version of the script has been further customized and personalized by
Amir Farhadi to cater to his specific speech recognition needs. 
"""

import datetime
import enum
import logging
import os.path
import sys
import threading
import tkinter as tk
import time
from pathlib import Path
from tkinter import messagebox, ttk

from dragonfly import Dictation, FuncContext, Function, Grammar, MappingRule, get_engine
from dragonfly.loader import CommandModuleDirectory
from dragonfly.log import setup_log

# watchdog is an optional dependency
# watchdog is a library for watching file system events
# pip install watchdog
try:
    from watchdog.events import RegexMatchingEventHandler
except:
    # define a stub
    class RegexMatchingEventHandler:
        pass

# Set user-specific configuration variables
try:
    from config import MODEL_DIRECTORY
    model_dir = MODEL_DIRECTORY
except ImportError:
    model_dir = 'kaldi_model'

# Configuration, of sorts (see also the kaldi invocation for choosing microphone and such)
MAX_DISPLAYED_HISTORY = 5


def try_prevent_window_activation_on_windows(tk_root):
    """Attempt to prevent the given TK window from stealing focus on Windows.

    CURRENTLY NOT WORKING!

    For some reason, both GetParent and GetAncestor return a handle of 00... even though it is
    crystal clear in Spy++ that there's a TK parent window. In fact, the wm_frame() of Tk() gives a
    handle that points at a "TkChild", and immediately above that is the parent we are trying to get
    at (class of TkTopLevel).. but we can't get a handle to it this way.

    What this means is that the rest of the function (which sets "no activate") will happily modify
    the child window (and we can see that's successful based on return codes as well as by looking
    at the modified styles in Spy++), but this has no effect on whether the window gets activated or
    not.

    TK itself *does* know about the top level window; aside from that it creates it, if we don't do
    `root.overrideredirect(True)` and instead do `self.root.wm_attributes("-toolwindow", 1)` then we
    can see the WS_EX_TOOLWINDOW attribute get set. It just doesn't provide a way to get at that via
    its API.
    """
    import ctypes
    from ctypes import windll, wintypes

    # flags we need
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CHILD = 0x40000000
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_NOACTIVATE = 0x08000000

    SWP_FRAMECHANGED = 0x0020
    SWP_NOACTIVATE = 0x0010
    SWP_NOZORDER = 0x0004
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001

    # functions we need
    GetWindowLong = windll.user32.GetWindowLongPtrW
    GetWindowLong.restype = wintypes.ULONG
    GetWindowLong.argtpes = (wintypes.HWND, wintypes.INT)

    SetWindowLong = windll.user32.SetWindowLongPtrW
    SetWindowLong.restype = wintypes.ULONG
    SetWindowLong.argtpes = (wintypes.HWND, wintypes.INT, wintypes.ULONG)

    SetWindowPos = windll.user32.SetWindowPos

    GetParent = windll.user32.GetParent
    GetParent.restype = wintypes.HWND
    GetParent.argtpes = (wintypes.HWND,)

    GetAncestor = windll.user32.GetAncestor
    GetAncestor.restype = wintypes.HWND
    GetAncestor.argtpes = (wintypes.HWND,)

    hwnd = int(tk_root.wm_frame(), 16)
    print("window handle is ", hwnd)

    # this call below doesn't work but should - see docstring
    # hwnd = GetAncestor(hwnd)
    # err = ctypes.get_last_error()
    # print("last error", err)
    # print("ancestor window is", hwnd)

    style = GetWindowLong(hwnd, GWL_EXSTYLE)
    err = ctypes.get_last_error()
    print("last error", err)
    print("existing style is", style)

    style = style | WS_EX_NOACTIVATE
    print("setting style of", style)

    res = SetWindowLong(hwnd, GWL_EXSTYLE, style)
    err = ctypes.get_last_error()
    print("last error", err)
    print("replaced style was", res)

    style = GetWindowLong(hwnd, GWL_EXSTYLE)
    err = ctypes.get_last_error()
    print("last error", err)
    print("new style is now", style)

    style = GetWindowLong(hwnd, GWL_EXSTYLE)
    err = ctypes.get_last_error()
    print("last error", err)
    print("existing style is", style)

    # docs say that SetWindowLong style changes can be cached until SetWindowPos is called.
    res = SetWindowPos(
        hwnd,
        0,
        1700,  # X coordinate
        600,   # Y coordinate
        0,
        0,
        SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE,
    )
    print("replaced window pos result", res)
    err = ctypes.get_last_error()
    print("last error", err)


class FakeStringVar:
    """A version of StringVar that can be used while tk is not yet loaded.

    Allows StringVar setters called from outside the tk thread to be agnostic of whether the UI has
    actually been created yet (normally you can't set the value of a StringVar if the tk root has
    not been created yet). Then, when the tk UI is actually created, a FakeStringVar can be
    'upgraded' into a real StringVar."""

    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value

    def upgrade(self):
        return tk.StringVar(value=self.value)


class App(threading.Thread):
    def __init__(self, do_quit):
        threading.Thread.__init__(self)
        self.daemon = True  # auto-quit the UI if the main app quits

        self.do_quit = do_quit
        self.context = {}

        self.status_line_var = FakeStringVar()
        self.last_heard_var = FakeStringVar()
        self.context_var = FakeStringVar()

        self.start()

    def set_status_line(self, s):
        """Update the displayed status (asleep, listening, etc)."""
        self.status_line_var.set(s)

    def set_last_heard(self, s):
        """Update the visual display of the last phrase heard."""
        self.last_heard_var.set(s)

    def set_visual_context(self, name, value):
        """Display a piece of visual context.

        This can be used to give the user a visual hint of current system status. For example, you
        could name the last few voice commands and display them here so they can be picked out for
        easy repetition. Or display the clipboard contents in each clipboard slot if you've rolled
        your own clipboard manager. Or show the surrounding words next to the cursor according to
        the accessibility API, to show whether the current app is accessible or not."""
        if value is None:
            del self.context[name]
        else:
            self.context[name] = value

        self.context_var.set(
            "\n".join(
                sorted((f"{name}: {value}" for name, value in self.context.items()))
            )
        )

    def _on_window_close(self):
        do_quit = messagebox.askyesno(
            message="Are you sure you want to quit KaldiUI?",
            icon="question",
            title="Quit?",
        )
        if do_quit:
            print("UI window closed - shutting down")
            self.do_quit()

    def run(self):
        self.root = tk.Tk()
        self.root.title("KaldiUI")
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        self.status_line_var = self.status_line_var.upgrade()
        label = ttk.Label(self.root, textvariable=self.status_line_var, font=("Arial", 7), wraplength=60)
        label.grid(column=0, row=0, sticky="nw")

        self.last_heard_var = self.last_heard_var.upgrade()
        label = ttk.Label(self.root, textvariable=self.last_heard_var)
        label.grid(column=1, row=0, sticky="nw")

        self.context_var = self.context_var.upgrade()
        label = ttk.Label(self.root, textvariable=self.context_var, wraplength=60, font=("Arial", 7))
        label.grid(column=0, row=1, columnspan=2, sticky="nw")

        self.root.attributes("-alpha", 0.8)  # transparency
        self.root.overrideredirect(True)  # hide the title bar
        self.root.wm_attributes("-topmost", 1)  # always on top

        # try_prevent_window_activation_on_windows(self.root)


        # in windows 10 i have a vertical taskbar on the right side
        # it is sized to its minimum width
        # i would like the tkinter window to be above the system tray 
        
        # Set window position
        x = 1858  # Desired X position
        y = 600  # Desired Y position
        self.root.geometry(f"+{x}+{y}")


        # ISSUE: if i click on the taskbar, the tkinter window disappears

        # initial quick attempts at bringing window back up top

        # attempt:1 it didn't work
        # def check_focus():
        #     if self.root.focus_get() is None:  # If the window is not in focus
        #         self.root.lift()  # Bring the window to the top
        # self.root.after(100, check_focus)  # Check again after 100ms

    #     self.root.after(100, check_focus)  # Start checking after 100ms

        # attempt:2
        #this didn't work
        # def bring_to_front(event):
        #     self.root.attributes('-topmost', 1)
        #     self.root.after_idle(self.root.attributes, '-topmost', 0)

        # # bring window back into focus
        # self.root.bind('<F12>', bring_to_front)


        self.root.mainloop()


class WatchDogFileChangeHandler(RegexMatchingEventHandler):
    def __init__(self, do_restart):
        RegexMatchingEventHandler.__init__(
            self,
            regexes=[r".+\.py"],
            ignore_regexes=[
                # VSCode's Black integration creates temp files like kaldi_main.py.80dd20e69f7d6eef4107c17b335180be.py
                # and if vscode runs Black on save, and we use those as a trigger to restart, we may restart before
                # the reformatted file is actually written, so we'll run old code in that case.
                # So as a workaround, ignore these temp files.
                # A more generic fix might be to wait until we see no more filesystem events for a time X before restarting,
                # but that would introduce a noticeable restart delay and would not be guaranteed to work either (say X is
                # less than the amount of time for a format operation to run - imagine big files).
                # A more reliable fix would be to MD5 everything right before restarting, save that to a file
                # somewhere, then check on startup restart if further changes have occurred.
                r".+\.py\.[a-f0-9]{32}\.py$"
            ],
            ignore_directories=True,
            case_sensitive=False,
        )
        self.last_modified = datetime.datetime.now()
        self.do_restart = do_restart

    def on_any_event(self, event):
        if datetime.datetime.now() - self.last_modified < datetime.timedelta(seconds=1):
            return

        self.last_modified = datetime.datetime.now()

        # TODO md5 all matching files to see if anything actually changed?

        print(f"Reloader: {event.src_path} {event.event_type}, restarting now...\n")
        self.do_restart()


def start_watchdog_observer(do_restart):
    try:
        from watchdog.observers import Observer
    except:
        print(
            "Reloader: watchdog not installed - run `pip install watchdog` to enable automatically restarting on code changes"
        )
        return None
    else:
        path = str(Path(".").resolve())
        event_handler = WatchDogFileChangeHandler(do_restart=do_restart)
        print(f"Reloader: watching {path} for changes...")

        observer = Observer()
        observer.schedule(event_handler, path, recursive=True)
        observer.start()
        return observer


class AppStatus(enum.Enum):
    LOADING = 1
    READY = 2
    SLEEPING = 3


sleeping = False


def load_sleep_wake_grammar(initial_awake, notify_status):
    sleep_grammar = Grammar("sleep")

    def sleep(force=False):
        global sleeping
        if not sleeping or force:
            sleeping = True
            sleep_grammar.set_exclusiveness(True)
        notify_status(AppStatus.SLEEPING)

    def wake(force=False):
        global sleeping
        if sleeping or force:
            sleeping = False
            sleep_grammar.set_exclusiveness(False)
        notify_status(AppStatus.READY)

    class SleepRule(MappingRule):
        mapping = {
            "start listening": Function(wake)
            + Function(lambda: get_engine().start_saving_adaptation_state()),
            "stop listening": Function(
                lambda: get_engine().stop_saving_adaptation_state()
            )
            + Function(sleep),
            "halt listening": Function(
                lambda: get_engine().stop_saving_adaptation_state()
            )
            + Function(sleep),
        }

    sleep_grammar.add_rule(SleepRule())

    sleep_noise_rule = MappingRule(
        name="sleep_noise_rule",
        mapping={"<text>": Function(lambda text: False and print(text))},
        extras=[Dictation("text")],
        context=FuncContext(lambda: sleeping),
    )
    sleep_grammar.add_rule(sleep_noise_rule)

    sleep_grammar.load()

    if initial_awake:
        wake(force=True)
    else:
        sleep(force=True)


def load_ui_grammar(do_quit, do_restart):
    ui_grammar = Grammar("KaldiUI")

    class ControlRule(MappingRule):
        mapping = {
            "please quit the kaldi UI": Function(do_quit),
            "please restart the kaldi UI": Function(do_restart),
        }

    ui_grammar.add_rule(ControlRule())

    ui_grammar.load()


def restart_process():
    import sys

    python = sys.executable
    os.execl(python, python, *sys.argv)


def main():
    try:
        path = os.path.dirname(__file__)
    except NameError:
        # The "__file__" name is not always available, for example
        # when this module is run from PythonWin.  In this case we
        # simply use the current working directory.
        path = os.getcwd()
        __file__ = os.path.join(path, "kaldi_module_loader_plus.py")

    # Set any configuration options here as keyword arguments.
    # See Kaldi engine documentation for all available options and more info.
    engine = get_engine(
        "kaldi",
        model_dir=model_dir,  # default model directory
        # vad_aggressiveness=0,  # default aggressiveness of VAD
        # vad_padding_start_ms=10,  # default ms of required silence before VAD
        vad_padding_end_ms=300,  # default ms of required silence after VAD
        # vad_complex_padding_end_ms=10,  # default ms of required silence after VAD for complex utterances
        # input_device_index=None,  # set to an int to choose a non-default microphone
        # lazy_compilation=True,  # set to True to parallelize & speed up loading
        # retain_dir=None,  # set to a writable directory path to retain recognition metadata and/or audio data
        # retain_audio=None,  # set to True to retain speech data wave files in the retain_dir (if set)
    )

    ui = App(do_quit=engine.disconnect)

    def notify_status(status: AppStatus):
        if status == AppStatus.LOADING:
            print("Loading...")
            ui.set_status_line("Initializing...")
        elif status == AppStatus.SLEEPING:
            print("Sleeping...")
            ui.set_status_line("Asleep...")
        elif status == AppStatus.READY:
            print("Awake...")
            ui.set_status_line("Listening...")
        else:
            print(f"Unknown status! {status}")

    notify_status(AppStatus.LOADING)

    # Call connect() now that the engine configuration is set.
    engine.connect()

    # Load grammars.
    load_sleep_wake_grammar(initial_awake=True, notify_status=notify_status)
    load_ui_grammar(do_quit=lambda: engine.disconnect(), do_restart=restart_process)
    directory = CommandModuleDirectory(path, excludes=[__file__])
    directory.load()

    # Define recognition callback functions.
    def on_begin():
        # ui.set_visual_context("last speech start", datetime.datetime.now().time())
        pass

    last_utterances = []

    def on_recognition(words):
        s = " ".join(words)
        if len(s):
            # ui.set_last_heard(f"Last heard: {s}")
            last_utterances.insert(0, s)  # Insert at the beginning of the list
            while len(last_utterances) > MAX_DISPLAYED_HISTORY:
                last_utterances.pop()  # Remove the last item from the list
            ui.set_visual_context("", "\n\n".join(last_utterances))
        print("Recognized: %s" % " ".join(words))

    def on_failure():
        # ui.set_visual_context("last speech failure", datetime.datetime.now().time())
        pass
 
    # Start the engine's main recognnition loop
    engine.prepare_for_recognition()
    watchdog_observer = start_watchdog_observer(do_restart=restart_process)
    try:
        notify_status(AppStatus.READY)
        engine.do_recognition(
            begin_callback=on_begin,
            recognition_callback=on_recognition,
            failure_callback=on_failure,
            end_callback=None,
            # post_recognition_callback=None,
        )
    except KeyboardInterrupt:
        print(f"Received keyboard interrupt so quitting...")

    # cleanup
    if watchdog_observer:
        watchdog_observer.stop()
        watchdog_observer.join()


if __name__ == "__main__":
    if False:
        # Debugging logging for reporting trouble
        logging.basicConfig(level=10)
        logging.getLogger("grammar.decode").setLevel(20)
        logging.getLogger("grammar.begin").setLevel(20)
        logging.getLogger("compound").setLevel(20)
        logging.getLogger("kaldi.compiler").setLevel(10)
    else:
        setup_log()

    try:
        main()
    except Exception as e:
        logging.exception(e)
        # wait for keypress in case we're in a new window
        input("Fatal error encountered; press Enter to exit...")