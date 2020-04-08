# Automatically compress videos in specified folder
# Only replace file if compressed video is smaller than original
# Remember and skip already compressed videos
# © Alexander Naehring, October 2019

import os
import tempfile
import subprocess
import re
import threading
import time
import shutil
import hashlib
import json
import platform
from tkinter import Tk, StringVar, DISABLED, TclError, PhotoImage
from tkinter.ttk import *
from tkinter import filedialog


def singleton(cls, *args, **kw):
    instances = {}

    def _singleton():
        if cls not in instances:
            instances[cls] = cls(*args, **kw)
        return instances[cls]
    return _singleton


# I know, singletons are bad!
# However, here I use a class for accessing, changing and storing the global program settings.
# As this program is small and there are not other modules using these settings, it should be fine.
# Also, there is in fact only a single file for storing those settings, multiple instances would not be realistic.
@singleton
class Settings:
    def __init__(self):
        print("init settings")
        if platform.system() == "Windows":
            self._app_folder: str = os.path.join(os.getenv('APPDATA'), "CompressVideo")
            if not os.path.exists(self._app_folder):
                os.makedirs(self._app_folder)
            self._settings_file: str = os.path.join(self._app_folder, "settings.json")
        else:
            raise NotImplementedError("Please specify a settings file location for this operating system")

        self._settings: dict = {}
        self.load_settings()

    def load_settings(self) -> bool:
        try:
            with open(self._settings_file) as json_file:
                self._settings = json.load(json_file)
                return True
        except FileNotFoundError:
            pass
        return False

    def write_settings(self):
        print("write settings to file %s" % self._settings_file)
        with open(self._settings_file, 'w') as file:
            json.dump(self._settings, file, indent=4, ensure_ascii=False)

    @property
    def extensions(self) -> list:
        return self._settings.get("extensions", [".mp4", ".mkv"])

    @extensions.setter
    def extensions(self, ext: list):
        self._settings["extensions"] = ext
        self.write_settings()

    @property
    def hash_list(self) -> list:
        return self._settings.get("hash_list", [])

    @hash_list.setter
    def hash_list(self, hash_list: list):
        self._settings["hash_list"] = hash_list
        self.write_settings()

    @property
    def last_folder(self) -> str:
        return self._settings.get("last_folder", "")

    @last_folder.setter
    def last_folder(self, last_folder: str):
        self._settings["last_folder"] = last_folder
        self.write_settings()


# ---------------------------------------------------------------------------------------------------------------
# Program functions
def get_filename(full_filename: str) -> str:
    """
    get file name without extension from full path
    :param full_filename: full path to file
    :return: file name without extension
    """
    import ntpath  # for windows paths to work even on *nix machines
    return ntpath.splitext(ntpath.basename(full_filename))[0]


def get_seconds(timestamp: str) -> int:
    """
    Return total number of seconds from HH:MM:SS timestamp
    :param timestamp: HH:MM:SS timestamp
    :return: total number of seconds
    """
    t = time.strptime(timestamp, "%H:%M:%S")
    return t.tm_sec + t.tm_min * 60 + t.tm_hour * 60 * 60


def get_file_fingerprint(filename: str) -> str:
    h = hashlib.md5()  # not secure, but fast
    with open(filename, 'rb') as file:
        while True:
            chunk = file.read(2**16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def process_daemon(path: str, parent_window) -> None:
    print(f"process_daemon: starting: {path}")
    try:  # use try-finally to clean up when function returns
        settings = Settings()

        filenames: list = []
        if os.path.isfile(path):
            if os.path.splitext(path)[1].casefold() in settings.extensions:
                filenames: list = [path]
        elif os.path.isdir(path):
            filenames: list = []
            path = os.path.abspath(path)
            for root, subs, files in os.walk(path):
                for filename in files:
                    if os.path.splitext(filename)[1].casefold() in settings.extensions:
                        filenames.append(os.path.join(root, filename))
        else:
            return

        if len(filenames) == 0:
            return
        print(f"process_daemon: found {len(filenames)} matching files:")
        for filename in filenames:
            print(f"\t{os.path.basename(filename)}")

        parent_window.progress_total["maximum"] = len(filenames)
        parent_window.progress_total["value"] = 1
        N = len(filenames)
        success = 0
        for f_id in range(N):
            filename = filenames[f_id]
            print(f"process_daemon: convert file {f_id+1}: {filename}")
            parent_window.label_video_name["text"] = os.path.basename(filename)

            success += hash_and_compress(filename, parent_window)

            parent_window.progress_total["value"] = f_id + 1
            # Attention: cannot set parent_window attributes while main thread is waiting for worker thread to join

            parent_window.label_progress["text"] = f"{success} videos compressed, {f_id-success+1} failed"

            if parent_window.stop:
                return

    finally:
        parent_window.btn_start_stop["text"] = "Start"
        parent_window.btn_browse["state"] = "normal"
        parent_window.path["state"] = "normal"
        parent_window.progress_total["value"] = 0
        parent_window.progress_video["value"] = 0
        if parent_window.stop:
            parent_window.label_video_name["text"] = "aborted"
        else:
            parent_window.label_video_name["text"] = "done"
        parent_window.ffmpeg_thread = None


def hash_and_compress(in_filename: str, parent_window) -> bool:
    # check if in file is in hash list

    settings = Settings()
    try:
        file_hash: str = get_file_fingerprint(in_filename)
        if file_hash in settings.hash_list:
            print('compress_file: file %s is already in hash list, skip' % in_filename)
            return True
    except FileNotFoundError:
        print(f"Could not find file '{in_filename}'")
        return False

    # start compression
    result: bool = compress_and_replace(in_filename, parent_window)

    if result:
        # in_filename is now compressed (or was already)
        # add file to hash list
        print('compress_file: add file to hash list')
        file_hash: str = get_file_fingerprint(in_filename)
        hash_list = settings.hash_list
        hash_list.append(file_hash)
        settings.hash_list = hash_list
    else:
        # ffmpeg returned with error (conversion not possible or aborted)
        pass

    return result


def compress_and_replace(in_filename: str, parent_window) -> bool:
    """
    execute ffmpeg conversion and check progress
    :return: True if file is, or was, compressed; False if compression failed
    :param parent_window: parent window object
    :param in_filename: original video to be converted
    """
    print(f"ffmpeg({in_filename})")
    settings = Settings()

    ext: str = os.path.splitext(in_filename)[1].casefold()
    assert ext in settings.extensions

    # print(f"ffmpeg: ffprobe file")
    cmd: list = ["ffprobe", "-hide_banner", in_filename]
    try:
        process = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise FileNotFoundError("ffprobe is not available in $PATH")

    codec = None
    w = None
    h = None
    crf = None

    for line in process.stderr.split("\n"):
        matches = re.findall(r"Stream #(\d+:\d+).*Video:.*, (\d+)x(\d+).*, (\d+(\.\d+)?) fps", line)
        if matches:
            matches = matches[0]  # list of tuples, as more than one group is in pattern, get tuple #0 from list
            stream = matches[0]
            w = int(matches[1])
            h = int(matches[2])
            fps = float(matches[3])

            # default codec and quality
            codec = "libx264"
            crf = "23"

            # use h265 for footage larger than WQHD
            if w*h > 2560*1440:
                codec = "libx265"
                # use CRF 23 for high framerate footage (>30 fps), default to CRF 25
                if fps > 30:
                    crf = "23"
                else:
                    crf = "25"
                break

            # use CRF 20 for footage larger than FHD or with high frame rate
            if w*h > 1920*1080 or fps > 30:
                codec = "libx264"
                crf = "20"
                break

    if not codec:
        print(process.stderr)
        print("Could not find video stream resolution metadata")
        return False

    print(f"ffmpeg: convert '{os.path.basename(in_filename)}' ({w}x{h}) using codec {codec} with CRF {crf}")
    tmp_filename: str = tempfile.gettempdir() + os.path.sep + get_filename(in_filename) + "_convert" + ext
    cmd: list = ["ffmpeg",
                 "-hide_banner", "-y",
                 "-i", in_filename,
                 "-map", "0",
                 "-map_metadata", "0",
                 "-c", "copy",
                 "-c:a", "libvorbis", "-aq", "4",
                 "-c:v", codec, "-crf", crf, "-preset", "slow",
                 "-max_muxing_queue_size", "4096",
                 "-movflags", "+faststart",
                 tmp_filename]
    try:
        process = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
                                   universal_newlines=True, cwd=None)
    except FileNotFoundError:
        raise FileNotFoundError("ffmpeg is not available in $PATH")

    total_time: int = 0

    parent_window.progress_video["maximum"] = 100
    parent_window.progress_video["value"] = 0
    lines = []

    while True:
        line: str = process.stderr.readline().rstrip()
        if line == '' and process.poll() is not None:
            break
        if parent_window.stop:
            # cancel compression
            process.terminate()

        if line:
            lines.append(line)
            # print(line)
            if not total_time:
                matches: list = re.findall(r"Duration: (\d\d:\d\d:\d\d)", line)
                if matches:
                    total_time = get_seconds(matches[0])
            else:
                matches: list = re.findall(r"time=(\d\d:\d\d:\d\d)", line)
                if matches:
                    current_time: int = get_seconds(matches[0])
                    percent: int = 100 * current_time // total_time
                    # print(f"ffmpeg: {percent}%")
                    parent_window.progress_video["value"] = percent

        time.sleep(0.01)

    exitcode = process.poll()
    if exitcode != 0:
        print(f"\tffmpeg error code {exitcode}")
        for line in lines:
            print(f"\t"+line)
        try:
            os.remove(tmp_filename)
        except FileNotFoundError:
            pass
        return False
    else:
        old_file_size: int = os.path.getsize(in_filename)
        new_file_size: int = os.path.getsize(tmp_filename)
        if new_file_size > 1024:
            #  print(old_file_size)
            #  print(new_file_size)
            print(f"\treduced file size by {100-100*new_file_size//old_file_size}%")
            if new_file_size < old_file_size * 0.9:
                # compressed video file is sufficiently smaller compared to original
                a_time = os.path.getatime(in_filename)
                m_time = os.path.getmtime(in_filename)
                os.utime(tmp_filename, (a_time, m_time))  # copy file modified date
                print("\treplace video with new file")
                # os.replace() # does not work when moving files between file systems
                os.remove(in_filename)  # shutil.move does not guarantee overwriting existing file
                shutil.move(tmp_filename, in_filename)  # move tmp file to original file location
                # os.remove(tmp_filename)
            else:
                # compressing does not make video file smaller
                print("\tkeep original video")
                os.remove(tmp_filename)
            # after conversion in_filename is compressed, or was already compressed so tmp is discarded
            return True

        else:
            print("\toutput file is very small, video conversion problem?")
            os.remove(tmp_filename)
            return False


# Open main window
class MainWindow:
    def __init__(self, parent):
        """
        Open main window
        """
        settings = Settings()

        self.parent = parent
        self.parent.withdraw()

        self.parent.title("Video Compression")
        self.parent.resizable(False, False)
        self.parent.iconphoto(False, PhotoImage(file="img/round_movie_black_48dp.png"))

        self.path_str = StringVar(value=settings.last_folder)
        self.path_str.trace_add("write", self.path_changed)
        self.path = Entry(self.parent, textvariable=self.path_str, width=50)
        self.path.grid(row=0, column=0, sticky='we', padx=5)
        self.btn_browse = Button(self.parent, text='Browse', command=self.browse)
        self.btn_browse.grid(row=0, column=1, padx=5)
        self.progress_video = Progressbar(self.parent, value=0, maximum=100, mode="determinate")
        self.progress_video.grid(row=1, column=0, columnspan=2, sticky='we', padx=5)
        self.progress_total = Progressbar(self.parent, value=0, maximum=100, mode="determinate")
        self.progress_total.grid(row=2, column=0, columnspan=2, sticky='we', padx=5)
        self.label_video_name = Label(self.parent, text='', anchor='w')
        self.label_video_name.grid(row=3, column=0)
        self.label_progress = Label(self.parent, text='', anchor='w')
        self.label_progress.grid(row=4, column=0)
        self.btn_start_stop = Button(self.parent, text='Start', state=DISABLED, command=self.start_stop)
        self.btn_start_stop.grid(row=3, rowspan=2, column=1)

        n_col, n_row = self.parent.grid_size()
        for col in range(n_col):
            self.parent.columnconfigure(col, weight=1)
        for row in range(n_row):
            self.parent.rowconfigure(row, weight=1, pad=5)

        self.ffmpeg_thread = None
        self.stop: bool = False

        self.parent.update()
        self.parent.eval(f"tk::PlaceWindow {self.parent.winfo_pathname(self.parent.winfo_id())} center")
        self.parent.deiconify()

    def browse(self):
        path: str = self.path_str.get()
        path = filedialog.askdirectory(initialdir=path, title="Select directory")
        if path:
            self.path_str.set(path)

    def path_changed(self, *args):
        # path string changed. Check for valid target and update start button
        self.check_path()
        settings = Settings()
        settings.last_folder = self.path_str.get()

    def check_path(self) -> bool:
        path: str = self.path_str.get()
        if os.path.isfile(path) or os.path.isdir(path):
            self.btn_start_stop["state"] = "normal"
            return True
        else:
            self.btn_start_stop["state"] = "disable"
            return False

    def start_stop(self) -> None:
        if self.ffmpeg_thread:
            # stop running thread
            self.stop = True
            # self.ffmpeg_thread.join()
            # Cannot wait for join, as ffmpeg_thread will change window properties,
            # which will cause deadlock if main thread is stuck in .join()
        else:
            # start new thread
            path: str = self.path_str.get()
            self.stop = False
            self.ffmpeg_thread = threading.Thread(target=process_daemon, args=[path, self])
            self.btn_start_stop["text"] = "Abort"
            self.btn_browse["state"] = "disable"
            self.path["state"] = "disable"
            self.ffmpeg_thread.start()

        # path: str = self.path_str.get()
        # if self.check_path():
        #     ffmpeg(path)
        # else:
        #     print(f"path not valid: {path}")


# main function, entry point
def main() -> int:
    """
    main entry point
    """
    # Create GUI
    tk_root = Tk()
    window = MainWindow(tk_root)

    # get clipboard string
    try:
        path: str = tk_root.clipboard_get()
    except TclError:
        path: str = ""

    # check if string is valid path
    if os.path.isfile(path) or os.path.isdir(path):
        window.path_str.set(path)
    # update GUI
    window.path_changed()

    # mainloop
    tk_root.mainloop()

    return 0


if __name__ == "__main__":
    exit(main())
