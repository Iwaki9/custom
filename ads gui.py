# ads.py
# Unified Edition v4 + Portable bin/ + GUI + HTML Preview Report

import argparse
import glob
import hashlib
import imagehash
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import random
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageStat

# --------------------------
# Portability: Auto-resolve bin/ folder
# --------------------------
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
BIN_DIR = os.path.join(BASE_DIR, "bin")

def get_tool(name):
    path = os.path.join(BIN_DIR, name)
    if os.path.isfile(path):
        return path
    return name.replace('.exe', '')

FFMPEG_BIN = get_tool("ffmpeg.exe")
FFPROBE_BIN = get_tool("ffprobe.exe")
RUBBERBAND_BIN = get_tool("rubberband.exe")
MAGICK_BIN = get_tool("magick.exe")

# --------------------------
# Utils
# --------------------------

def run_cmd(cmd: str, quiet: bool = True) -> subprocess.CompletedProcess:
    kwargs = dict(shell=True, text=True)
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def check_output(cmd: str) -> str:
    return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()


def open_folder(path: str):
    try:
        abspath = os.path.abspath(path)
        if platform.system().lower().startswith("win"):
            os.startfile(abspath)
        elif platform.system().lower() == "darwin":
            run_cmd(f'open "{abspath}"', quiet=True)
        else:
            run_cmd(f'xdg-open "{abspath}"', quiet=True)
    except Exception:
        pass


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def is_sorted(arr: List[int]) -> bool:
    for i in range(len(arr) - 1):
        if arr[i] > arr[i + 1]:
            return False
    return True


def _ff_path(p: str) -> str:
    s = p.replace("\\", "/")
    if len(s) >= 3 and s[1] == ":" and s[2] == "/" and s[0].isalpha():
        s = s[0] + "\\:" + s[2:]
    return s


def _ff_out_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def _ff_concat_path(p: str) -> str:
    s = _ff_out_path(p)
    return s.replace("'", "\'")


def cleanup_workdir_temp(workdir: str) -> None:
    try:
        candidates = [
            os.path.join(workdir, "frames"),
            os.path.join(workdir, "_tmp_cut"),
            os.path.join(workdir, "intermediate"),
            os.path.join(workdir, "audio"),
            os.path.join(workdir, "timemap"),
        ]
        for p in candidates:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p


# --------------------------
# Progress (for GUI)
# --------------------------

_PROGRESS_ENABLED = False


def set_progress_enabled(enabled: bool) -> None:
    global _PROGRESS_ENABLED
    _PROGRESS_ENABLED = bool(enabled)
    if _PROGRESS_ENABLED:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def progress_emit(phase: str, p: float, msg: str = "", step: str = "") -> None:
    if not _PROGRESS_ENABLED:
        return
    try:
        payload = {"phase": phase, "p": float(p), "step": step or "", "msg": msg or ""}
        print("@@PROGRESS@@ " + json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:
        pass


# --------------------------
# Global ffmpeg concurrency limiter
# --------------------------

_FFMPEG_SEMAPHORE = None


class _FFmpegSemaphore:
    def __init__(self, max_procs: int, lock_dir: str, stale_after_s: float = 24 * 3600):
        self.max_procs = max(0, int(max_procs))
        self.lock_dir = lock_dir
        self.stale_after_s = float(stale_after_s)
        ensure_dir(lock_dir)

    def _slot_path(self, i: int) -> str:
        return os.path.join(self.lock_dir, f'ffmpeg_slot_{i}.lock')

    def _is_stale(self, p: str) -> bool:
        try:
            st = os.stat(p)
            age = time.time() - st.st_mtime
            return age > self.stale_after_s
        except Exception:
            return False

    @contextmanager
    def acquire(self, purpose: str = ''):
        if self.max_procs <= 0:
            yield
            return

        slot = None
        while slot is None:
            for i in range(self.max_procs):
                p = self._slot_path(i)
                try:
                    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(json.dumps({'pid': os.getpid(), 'ts': time.time(), 'purpose': purpose}, ensure_ascii=False))
                    slot = p
                    break
                except FileExistsError:
                    if self._is_stale(p):
                        try: os.remove(p)
                        except Exception: pass
                    continue
                except (PermissionError, IsADirectoryError):
                    try:
                        if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                        elif self._is_stale(p): os.remove(p)
                    except Exception: pass
                    continue

            if slot is None:
                time.sleep(0.15 + random.random() * 0.10)

        try:
            yield
        finally:
            try:
                if slot and os.path.exists(slot): os.remove(slot)
            except Exception: pass


def init_ffmpeg_limiter(max_procs: int, lock_dir: Optional[str] = None) -> None:
    global _FFMPEG_SEMAPHORE
    try: max_procs_i = int(max_procs)
    except Exception: max_procs_i = 0
    if max_procs_i <= 0:
        _FFMPEG_SEMAPHORE = None
        return
    if not lock_dir:
        lock_dir = os.path.join(tempfile.gettempdir(), 'ads_ffmpeg_semaphore')
    ensure_dir(lock_dir)
    _FFMPEG_SEMAPHORE = _FFmpegSemaphore(max_procs=max_procs_i, lock_dir=str(lock_dir))


def _run_shell(cmd: str, *, quiet: bool, capture_output: bool) -> subprocess.CompletedProcess:
    kwargs = dict(shell=True, text=True)
    if capture_output: kwargs['capture_output'] = True
    elif quiet:
        kwargs['stdout'] = subprocess.DEVNULL
        kwargs['stderr'] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def ffmpeg_run(cmd: str, *, quiet: bool = True, capture_output: bool = False, purpose: str = '') -> subprocess.CompletedProcess:
    global _FFMPEG_SEMAPHORE
    if _FFMPEG_SEMAPHORE is None:
        return _run_shell(cmd, quiet=quiet, capture_output=capture_output)
    with _FFMPEG_SEMAPHORE.acquire(purpose=purpose or 'ffmpeg'):
        return _run_shell(cmd, quiet=quiet, capture_output=capture_output)


def safe_basename(path: str) -> str:
    b = os.path.basename(path)
    b = re.sub(r'[^0-9A-Za-zА-Яа-я._ -]+', '_', b)
    return b[:120]


def job_id_from_paths(sp: str, tp: str) -> str:
    h = hashlib.md5((os.path.abspath(sp) + "||" + os.path.abspath(tp)).encode("utf-8")).hexdigest()[:10]
    return h


# --------------------------
# ffprobe helpers
# --------------------------

def get_duration(video_path: str) -> float:
    cmd = f'"{FFPROBE_BIN}" -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    return float(check_output(cmd))


def get_start_time(video_path: str) -> float:
    cmd = f'"{FFPROBE_BIN}" -v error -show_entries format=start_time -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    try:
        return float(check_output(cmd))
    except Exception:
        return 0.0


def get_fps(video_path: str) -> float:
    cmd = f'"{FFPROBE_BIN}" -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    s = check_output(cmd)
    if "/" in s:
        num, den = s.split("/", 1)
        return float(num) / float(den)
    return float(s)


def get_tbn(video_path: str) -> float:
    cmd = f'"{FFPROBE_BIN}" -v error -select_streams v:0 -show_entries stream=time_base -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    s = check_output(cmd)
    if "/" in s:
        num, den = s.split("/", 1)
        num_f = float(num)
        den_f = float(den)
        if num_f == 0:
            raise ValueError("Invalid time_base")
        return den_f / num_f
    return float(s)


def get_audio_hz(video_path: str) -> int:
    cmd = f'"{FFPROBE_BIN}" -v error -select_streams a:0 -show_entries stream=sample_rate -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    return int(check_output(cmd))


def has_audio_stream(video_path: str) -> bool:
    try:
        cmd = f'"{FFPROBE_BIN}" -v error -select_streams a -show_entries stream=index -of csv=p=0 "{video_path}"'
        out = check_output(cmd)
        return bool(out.strip())
    except Exception:
        return False

def normalize_media_timestamps(input_path: str, output_path: str, ffmpeg_script: str = "ffmpeg") -> str:
    try:
        st = float(get_start_time(input_path))
    except Exception:
        st = 0.0
    if abs(st) <= 0.05:
        return input_path
    ensure_dir(os.path.dirname(output_path) or ".")
    ext = os.path.splitext(output_path)[1].lower()
    movflags = " -movflags +faststart" if ext in (".mp4", ".m4v", ".mov") else ""
    cmd = f'{ffmpeg_script} -y -fflags +genpts -i "{input_path}" -map 0 -c copy -avoid_negative_ts make_zero{movflags} "{output_path}"'
    ffmpeg_run(cmd, quiet=True, purpose="normalize_ts")
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path
    return input_path

def normalize_video_timestamps_only(input_path: str, output_path: str, ffmpeg_script: str = "ffmpeg", force: bool = True) -> str:
    if not force:
        try:
            st = float(get_start_time(input_path))
            if abs(st) <= 0.05:
                return input_path
        except Exception:
            return input_path
    ensure_dir(os.path.dirname(output_path) or ".")
    ext = os.path.splitext(output_path)[1].lower()
    movflags = " -movflags +faststart" if ext in (".mp4", ".m4v", ".mov") else ""
    cmd = f'{ffmpeg_script} -y -fflags +genpts -i "{input_path}" -map 0:v:0 -c copy -avoid_negative_ts make_zero{movflags} "{output_path}"'
    ffmpeg_run(cmd, quiet=True, purpose="normalize_ts_video")
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path
    return input_path

# --------------------------
# Preview helpers
# --------------------------

def save_cut_segment_video(video_path: str, start_time: float, end_time: float, output_file: str,
                           ffmpeg_script: str = "ffmpeg") -> Optional[str]:
    ensure_dir(os.path.dirname(output_file) or ".")
    start_time = max(0.0, float(start_time))
    end_time = max(start_time, float(end_time))
    cmd_copy = f'{ffmpeg_script} -y -ss {start_time:.3f} -to {end_time:.3f} -i "{video_path}" -map 0:v:0 -map 0:a? -c copy "{output_file}"'
    ffmpeg_run(cmd_copy, quiet=True, purpose='preview_copy')
    if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
        return output_file
    try:
        if os.path.exists(output_file):
            os.remove(output_file)
    except Exception:
        pass
    cmd_enc = (
        f'{ffmpeg_script} -y -ss {start_time:.3f} -to {end_time:.3f} -i "{video_path}" '
        f'-map 0:v:0 -map 0:a? -c:v libx264 -preset ultrafast -crf 28 '
        f'-c:a aac -b:a 128k -movflags +faststart "{output_file}"'
    )
    ffmpeg_run(cmd_enc, quiet=True, purpose='preview_encode')
    if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
        return output_file
    return None

def save_preview_frames(video_path: str, start_time: float, end_time: float, output_folder: str,
                        num_frames: int = 8, ffmpeg_script: str = "ffmpeg") -> List[str]:
    ensure_dir(output_folder)
    duration = max(0.001, end_time - start_time)
    interval = duration / (num_frames + 1)
    saved = []
    for i in range(1, num_frames + 1):
        ts = start_time + interval * i
        out = os.path.join(output_folder, f"frame_{i:02d}_at_{ts:.2f}s.jpg")
        cmd = f'{ffmpeg_script} -y -ss {ts:.3f} -i "{video_path}" -vframes 1 -q:v 2 "{out}"'
        ffmpeg_run(cmd, quiet=True, purpose='preview_frame')
        if os.path.exists(out):
            saved.append(out)
    return saved


def save_edge_previews(video_path: str, start_time: float, end_time: float, duration: float,
                       out_dir: str, tag: str, ffmpeg_script: str) -> Dict[str, str]:
    ensure_dir(out_dir)
    clips: Dict[str, str] = {}
    if start_time > 0.15:
        a = max(0.0, start_time - 3.0)
        b = start_time
        p = os.path.join(out_dir, f"{tag}_START_REMOVED_last3s.mp4")
        if save_cut_segment_video(video_path, a, b, p, ffmpeg_script):
            clips["start_removed"] = p
    c = max(0.0, start_time)
    d = min(duration, c + 3.0)
    if d - c > 0.05:
        p = os.path.join(out_dir, f"{tag}_START_KEPT_first3s.mp4")
        if save_cut_segment_video(video_path, c, d, p, ffmpeg_script):
            clips["start_kept"] = p
    a = max(0.0, min(duration, end_time) - 3.0)
    b = min(duration, end_time)
    if b - a > 0.05:
        p = os.path.join(out_dir, f"{tag}_END_KEPT_last3s.mp4")
        if save_cut_segment_video(video_path, a, b, p, ffmpeg_script):
            clips["end_kept"] = p
    if end_time < duration - 0.15:
        c = end_time
        d = min(duration, end_time + 3.0)
        if d - c > 0.05:
            p = os.path.join(out_dir, f"{tag}_END_REMOVED_first3s.mp4")
            if save_cut_segment_video(video_path, c, d, p, ffmpeg_script):
                clips["end_removed"] = p
    return clips

def concat_two_clips(clip_a: str, clip_b: str, out_path: str, ffmpeg_script: str) -> bool:
    try:
        ensure_dir(os.path.dirname(out_path))
        lst = out_path + '.concat.txt'
        with open(lst, 'w', encoding='utf-8') as f:
            f.write("file '{}'\n".format(clip_a.replace("'", "'\\''")))
            f.write("file '{}'\n".format(clip_b.replace("'", "'\\''")))
        cmd = (
            f'{ffmpeg_script} -y -hide_banner -loglevel error '
            f'-f concat -safe 0 -i "{lst}" '
            f'-c:v libx264 -crf 18 -preset veryfast -c:a aac -b:a 128k '
            f'"{out_path}"'
        )
        res = ffmpeg_run(cmd, quiet=True, capture_output=True, purpose='preview_concat')
        try:
            os.remove(lst)
        except Exception:
            pass
        return res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False

def capture_frame_info(video_path: str, output_folder: str, cut_borders: bool, frame_diff: float,
                       video_tbn: float, video_fps: float, video_pos_per_frame: float,
                       audio_samples_per_frame: float, ffmpeg_script: str, imagemagick_script: str,
                       keep_frame_dumps: bool = False) -> List[dict]:
    ensure_dir(output_folder)
    time_txt = os.path.join(output_folder, "time.txt")
    time_txt_ff = _ff_path(time_txt)
    out_pattern_ff = _ff_out_path(os.path.join(output_folder, "img%05d.jpg"))
    vf = f"select='gt(scene,{frame_diff/100})',metadata=mode=print:file='{time_txt_ff}'"
    cmd = f'{ffmpeg_script} -y -hide_banner -loglevel error -i "{video_path}" -vf "{vf}" -vsync vfr "{out_pattern_ff}"'
    res = ffmpeg_run(cmd, quiet=False, capture_output=True, purpose='scene_detect')
    if res.returncode != 0:
        print("❌ ffmpeg error while detecting scenes:")
        if res.stderr:
            print(res.stderr[:1600])
        return []
    if not os.path.exists(time_txt):
        print(f"⚠️ ffmpeg did not create {time_txt}.")
        return []
    with open(time_txt, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    frame_info: List[dict] = []
    for line in lines:
        match = re.match(r"frame:(\d+)\s+pts:(\d+)\s+pts_time:(\d+.?\d*)", line.strip())
        if not match:
            continue
        orig_frame_index = int(match.group(1))
        pts = int(match.group(2))
        pts_time = float(match.group(3))
        scene_frame_index = len(frame_info)
        full_video_index = int(round(pts / video_pos_per_frame)) if video_pos_per_frame else 0
        frame_index_in_its_second = round(full_video_index % video_fps) if video_fps else 0
        audio_sample = audio_samples_per_frame * (pts / video_pos_per_frame) if audio_samples_per_frame and video_pos_per_frame else 0.0
        img_path = os.path.join(output_folder, f"img{scene_frame_index + 1:05d}.jpg")
        if cut_borders and os.path.exists(img_path):
            cmd2 = (
                f'{imagemagick_script} mogrify -fuzz 4% -define trim:percent-background=0% '
                f'-trim +repage "{img_path}"'
            )
            subprocess.run(cmd2, shell=True, capture_output=True)
        h = None
        try:
            if os.path.exists(img_path):
                img = Image.open(img_path)
                h = imagehash.average_hash(img)
        except Exception:
            h = None
        frame_info.append({
            "scene_frame_index": scene_frame_index,
            "orig_frame": orig_frame_index,
            "index": full_video_index,
            "second_index": frame_index_in_its_second,
            "pts": pts,
            "pts_s": pts / video_tbn if video_tbn else pts_time,
            "pts_time": pts_time,
            "audio_sample": audio_sample,
            "hash": h
        })
        if not keep_frame_dumps:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except Exception:
                pass
    if not keep_frame_dumps:
        try:
            os.remove(time_txt)
        except Exception:
            pass
    return frame_info


# --------------------------
# Twin frames / sync / jumps
# --------------------------

def find_twin_frames(main_frame_infos: List[dict], brothers_frame_infos: List[dict], reverse_main_and_twin: bool = False, max_distance: int = None) -> List[dict]:
    pairs = []
    for main in main_frame_infos:
        cur = {"main": main["scene_frame_index"], "twin": None, "distance": float("inf")}
        for bro in brothers_frame_infos:
            if main["hash"] is None or bro["hash"] is None:
                continue
            d = main["hash"] - bro["hash"]
            if d < cur["distance"]:
                cur["distance"] = d
                cur["twin"] = bro["scene_frame_index"]
        if cur["twin"] is not None:
            if max_distance is None or cur["distance"] <= max_distance:
                pairs.append(cur)
    double_twin_indexes: Dict[int, List[int]] = {}
    bad_indexes: List[int] = []
    possibly_bad_side = "main" if reverse_main_and_twin else "twin"
    for idx, pair in enumerate(pairs):
        if reverse_main_and_twin:
            tmp = pair["twin"]
            pair["twin"] = pair["main"]
            pair["main"] = tmp
        key = pair[possibly_bad_side]
        if key is None:
            bad_indexes.append(idx)
            continue
        double_twin_indexes.setdefault(key, []).append(idx)
    for vals in double_twin_indexes.values():
        if len(vals) > 1:
            bad_indexes += vals
    for bi in sorted(set(bad_indexes), reverse=True):
        if 0 <= bi < len(pairs):
            pairs.pop(bi)
    bad_indexes = []
    for i in range(len(pairs)):
        if i > 0 and pairs[i][possibly_bad_side] < pairs[i - 1][possibly_bad_side]:
            bad_indexes.append(i)
        if i < len(pairs) - 1 and pairs[i][possibly_bad_side] > pairs[i + 1][possibly_bad_side]:
            bad_indexes.append(i)
    for bi in sorted(set(bad_indexes), reverse=True):
        if 0 <= bi < len(pairs):
            pairs.pop(bi)
    pairs_twin_indexes = [p[possibly_bad_side] for p in pairs if p.get(possibly_bad_side) is not None]
    if pairs_twin_indexes and not is_sorted(pairs_twin_indexes):
        print("⚠️ Unordered frames detected -> greedy filter")
        new_pairs = []
        last_val = -1
        for p in pairs:
            val = p.get(possibly_bad_side)
            if val is not None and val > last_val:
                new_pairs.append(p)
                last_val = val
        print(f"   Pairs: {len(pairs)} -> {len(new_pairs)}")
        pairs = new_pairs
    return pairs


def auto_find_sync_points(twins: List[dict], source_frame_info: Dict[int, dict], target_frame_info: Dict[int, dict], max_distance: int = 5):
    start_pair = 0
    for i, pair in enumerate(twins):
        if pair["distance"] <= max_distance:
            start_pair = i
            break
    end_pair = len(twins) - 1
    for i in range(len(twins) - 1, -1, -1):
        if twins[i]["distance"] <= max_distance:
            end_pair = i
            break
    return start_pair, end_pair


def detect_time_jumps_hybrid(
    twins: List[dict],
    source_frame_info: Dict[int, dict],
    target_frame_info: Dict[int, dict],
    small_threshold: float = 2.0,
    large_threshold: float = 5.0,
    micro_ignore: float = 0.10,
    min_insert_cut: float = 3.0,
    prefer_longer_cuts: bool = True,
    duration_bias: float = 0.0,
    min_insert_cut_shorter: Optional[float] = None,
):
    all_jumps: List[dict] = []
    stretch_segments: List[dict] = []
    cut_segments: List[dict] = []
    longer_video: Optional[str] = None
    shorter_min_cut = None
    try:
        if prefer_longer_cuts and abs(float(duration_bias)) > 1.0:
            longer_video = "source" if float(duration_bias) > 0 else "target"
            shorter_min_cut = float(min_insert_cut_shorter) if (min_insert_cut_shorter is not None) else float(large_threshold)
    except Exception:
        longer_video = None
        shorter_min_cut = None
    for i in range(len(twins) - 1):
        cur = twins[i]
        nxt = twins[i + 1]
        sc = float(source_frame_info[cur["main"]]["pts_s"])
        sn = float(source_frame_info[nxt["main"]]["pts_s"])
        sd = sn - sc
        tc = float(target_frame_info[cur["twin"]]["pts_s"])
        tn = float(target_frame_info[nxt["twin"]]["pts_s"])
        td = tn - tc
        signed = td - sd
        diff = abs(signed)
        if diff <= micro_ignore:
            continue
        jump = {
            "pair_index": i,
            "source_delta": sd,
            "target_delta": td,
            "difference": diff,
            "difference_raw": diff,
            "source_start": sc,
            "source_end": sn,
            "target_start": tc,
            "target_end": tn,
        }
        if signed > 0:
            jump["video"] = "target"
            jump["insert_duration"] = signed
        else:
            jump["video"] = "source"
            jump["insert_duration"] = -signed
        is_cut = False
        if diff >= large_threshold:
            is_cut = True
        elif diff >= small_threshold:
            if float(jump["insert_duration"]) >= float(min_insert_cut):
                is_cut = True
        if is_cut and longer_video and jump.get("video") != longer_video and shorter_min_cut is not None:
            try:
                ins = float(jump.get("insert_duration", 0.0) or 0.0)
                if ins < float(shorter_min_cut) and diff < float(large_threshold):
                    is_cut = False
                    jump["downgraded_from_cut"] = True
                    jump["downgrade_reason"] = f"prefer_longer_cuts(shorter_min_cut={float(shorter_min_cut):.3f})"
            except Exception:
                pass
        if is_cut:
            jump["action"] = "cut"
            cut_segments.append(jump)
        else:
            jump["action"] = "stretch"
            stretch_segments.append(jump)
        all_jumps.append(jump)
    return all_jumps, cut_segments, stretch_segments


# --------------------------
# Micro-verify short CUT candidates
# --------------------------

def _parse_float_list_csv(s: Optional[str]) -> List[float]:
    if not s:
        return []
    out: List[float] = []
    for part in re.split(r"[,\s]+", str(s).strip()):
        if not part:
            continue
        try:
            out.append(float(part))
        except Exception:
            pass
    return out


def _times_grid(start: float, end: float, step: float, jitters: List[float]) -> List[float]:
    start = float(start); end = float(end)
    step = max(0.001, float(step))
    if end < start:
        start, end = end, start
    times: List[float] = []
    t = start
    while t <= end + 1e-6:
        if jitters:
            for j in jitters:
                times.append(t + float(j))
        else:
            times.append(t)
        t += step
    uniq = sorted(set(round(max(start, min(end, x)), 3) for x in times))
    return [float(x) for x in uniq]


def micro_verify_cut_candidate(
    jump: dict,
    *,
    source_path: str,
    target_path: str,
    source_cut_borders: bool,
    target_cut_borders: bool,
    ffmpeg_script: str,
    imagemagick_script: str,
    temp_dir: str,
    region_mode: str = "multi",
    quality_threshold: float = 1.0,
    hash_threshold: int = 10,
    sample_step: float = 0.10,
    sample_jitters: Optional[str] = "0,0.05",
    search_window: float = 0.60,
    keep_cut_max_match_rate: float = 0.20,
    min_unmatched_run_s: float = 0.60,
) -> dict:
    try:
        video = str(jump.get("video") or "")
        src_start = float(jump.get("source_start", 0.0))
        src_end = float(jump.get("source_end", 0.0))
        tgt_start = float(jump.get("target_start", 0.0))
        tgt_end = float(jump.get("target_end", 0.0))
        insert_dur = float(jump.get("insert_duration", 0.0) or 0.0)
    except Exception:
        return {"decision": "downgrade_to_stretch", "reason": "bad_jump_fields"}
    if insert_dur <= 0.0:
        return {"decision": "downgrade_to_stretch", "reason": "non_positive_insert"}
    if video == "target":
        long_path, long_start, long_end, long_cb = target_path, tgt_start, tgt_end, bool(target_cut_borders)
        short_path, short_start, short_end, short_cb = source_path, src_start, src_end, bool(source_cut_borders)
    else:
        long_path, long_start, long_end, long_cb = source_path, src_start, src_end, bool(source_cut_borders)
        short_path, short_start, short_end, short_cb = target_path, tgt_start, tgt_end, bool(target_cut_borders)
    long_len = float(long_end - long_start)
    short_len = float(short_end - short_start)
    if long_len <= 0.3 or short_len <= 0.3:
        return {"decision": "downgrade_to_stretch", "reason": "too_short_context"}
    jitters = _parse_float_list_csv(sample_jitters)
    times_long = _times_grid(long_start, long_end, sample_step, jitters)
    times_short = _times_grid(short_start, short_end, sample_step, jitters)
    if len(times_long) < 6 or len(times_short) < 6:
        return {"decision": "downgrade_to_stretch", "reason": "insufficient_samples"}
    sc_long = EdgeScanner(
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        cut_borders=long_cb,
        temp_dir=temp_dir,
        region_mode=region_mode,
        quality_threshold=quality_threshold,
    )
    sc_short = EdgeScanner(
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        cut_borders=short_cb,
        temp_dir=temp_dir,
        region_mode=region_mode,
        quality_threshold=quality_threshold,
    )
    feats_long: Dict[float, Optional[dict]] = {t: sc_long.get_features(long_path, t) for t in times_long}
    feats_short: Dict[float, Optional[dict]] = {t: sc_short.get_features(short_path, t) for t in times_short}
    short_sorted = sorted(times_short)
    valid = 0
    matched = 0
    max_unmatched = 0
    cur_unmatched = 0
    dists: List[int] = []
    for t in sorted(times_long):
        fa = feats_long.get(t)
        if not fa or not isinstance(fa, dict):
            continue
        try:
            if float(fa.get("_meta", {}).get("max_q", 0.0)) < float(quality_threshold):
                continue
        except Exception:
            pass
        rel = (t - long_start) / max(1e-9, long_len)
        pred = short_start + rel * short_len
        lo = bisect_left(short_sorted, pred - float(search_window))
        hi = bisect_right(short_sorted, pred + float(search_window))
        best: Optional[int] = None
        for ts in short_sorted[lo:hi]:
            fb = feats_short.get(ts)
            if not fb or not isinstance(fb, dict):
                continue
            try:
                if float(fb.get("_meta", {}).get("max_q", 0.0)) < float(quality_threshold):
                    continue
            except Exception:
                pass
            dist = sc_long.distance(fa, fb, int(hash_threshold))
            if dist is None:
                continue
            if best is None or dist < best:
                best = dist
        if best is None:
            continue
        valid += 1
        dists.append(int(best))
        if int(best) <= int(hash_threshold):
            matched += 1
            cur_unmatched = 0
        else:
            cur_unmatched += 1
            if cur_unmatched > max_unmatched:
                max_unmatched = cur_unmatched
    if valid <= 0:
        return {"decision": "downgrade_to_stretch", "reason": "no_valid_samples"}
    match_rate = float(matched) / float(valid)
    max_unmatched_run_s = float(max_unmatched) * float(sample_step)
    keep = (match_rate <= float(keep_cut_max_match_rate)) and (max_unmatched_run_s >= float(min_unmatched_run_s))
    return {
        "decision": "keep_cut" if keep else "downgrade_to_stretch",
        "match_rate": match_rate,
        "valid": int(valid),
        "matched": int(matched),
        "hash_threshold": int(hash_threshold),
        "sample_step": float(sample_step),
        "jitters": jitters,
        "search_window": float(search_window),
        "max_unmatched_run_s": max_unmatched_run_s,
        "insert_duration": float(insert_dur),
        "dists_min": int(min(dists)) if dists else None,
        "dists_med": int(sorted(dists)[len(dists)//2]) if dists else None,
        "dists_max": int(max(dists)) if dists else None,
        "reason": ("strong_insert_signature" if keep else "looks_like_drift_or_stretch"),
    }

# --------------------------
# Tracking Deep Scan edges
# --------------------------

@dataclass
class EdgeScanner:
    ffmpeg_script: str
    imagemagick_script: str
    cut_borders: bool
    temp_dir: str
    region_mode: str = "multi"
    quality_threshold: float = 1.0
    _feat_cache: Dict[str, dict] = None

    def __post_init__(self):
        if self._feat_cache is None:
            self._feat_cache = {}

    def _extract_frame(self, video_path: str, timestamp: float) -> Optional[str]:
        ts = max(0.0, float(timestamp))
        safe_name = f"{abs(hash((video_path, round(ts, 3)))):x}.jpg"
        img_path = os.path.join(self.temp_dir, safe_name)
        if not os.path.exists(img_path):
            cmd = f'{self.ffmpeg_script} -y -ss {ts:.3f} -i "{video_path}" -vframes 1 -q:v 2 "{img_path}"'
            ffmpeg_run(cmd, quiet=True, purpose='preview_frame')
            if self.cut_borders and os.path.exists(img_path):
                cmd2 = (
                    f'{self.imagemagick_script} mogrify -fuzz 4% -define trim:percent-background=0% '
                    f'-trim +repage "{img_path}"'
                )
                run_cmd(cmd2, quiet=True)
        if not os.path.exists(img_path):
            return None
        return img_path

    def _quality(self, img: Image.Image) -> float:
        g = img.convert("L").resize((32, 32))
        stat = ImageStat.Stat(g)
        return float(stat.stddev[0])

    def _region_boxes(self, w: int, h: int) -> List[Tuple[str, Tuple[int, int, int, int]]]:
        boxes: List[Tuple[str, Tuple[int, int, int, int]]] = []
        boxes.append(("full", (0, 0, w, h)))
        cx0 = int(w * 0.15)
        cy0 = int(h * 0.15)
        cx1 = int(w * 0.85)
        cy1 = int(h * 0.85)
        boxes.append(("center", (cx0, cy0, cx1, cy1)))
        boxes.append(("top", (0, 0, w, int(h * 0.5))))
        boxes.append(("bottom", (0, int(h * 0.5), w, h)))
        return boxes

    def get_features(self, video_path: str, timestamp: float) -> Optional[dict]:
        ts = max(0.0, float(timestamp))
        key = f"{video_path}|{round(ts, 3)}|{int(self.cut_borders)}|{self.region_mode}|{self.quality_threshold}"
        if key in self._feat_cache:
            return self._feat_cache[key]
        img_path = self._extract_frame(video_path, ts)
        if img_path is None:
            return None
        try:
            img = Image.open(img_path)
            w, h = img.size
            feats: Dict[str, dict] = {}
            regions = []
            if self.region_mode == "full":
                regions = [("full", (0, 0, w, h))]
            elif self.region_mode == "center":
                regions = [self._region_boxes(w, h)[1]]
            else:
                regions = self._region_boxes(w, h)
            qs = []
            for name, box in regions:
                reg = img.crop(box)
                q = self._quality(reg)
                qs.append(q)
                feats[name] = {"hash": imagehash.phash(reg), "q": q}
            feats["_meta"] = {
                "max_q": max(qs) if qs else 0.0,
                "avg_q": (sum(qs) / len(qs)) if qs else 0.0
            }
            self._feat_cache[key] = feats
            return feats
        except Exception:
            return None

    def distance(self, feats_a: dict, feats_b: dict, hash_threshold: int) -> Optional[int]:
        best = None
        keys_a = set(feats_a.keys()) - {"_meta"}
        keys_b = set(feats_b.keys()) - {"_meta"}
        common = keys_a & keys_b
        if not common:
            return None
        for k in common:
            qa = float(feats_a[k]["q"])
            qb = float(feats_b[k]["q"])
            if self.quality_threshold > 0 and (qa < self.quality_threshold or qb < self.quality_threshold):
                continue
            d = feats_a[k]["hash"] - feats_b[k]["hash"]
            if best is None or d < best:
                best = d
        if best is not None:
            return int(best)
        for k in common:
            d = feats_a[k]["hash"] - feats_b[k]["hash"]
            if best is None or d < best:
                best = d
        return int(best) if best is not None else None


def verify_end_similarity(
    source_path: str,
    target_path: str,
    source_duration: float,
    target_duration: float,
    ffmpeg_script: str,
    imagemagick_script: str,
    region_mode: str = "multi",
    quality_threshold: float = 1.0,
    hash_threshold: int = 16,
    window: float = 15.0,
    samples: int = 4,
) -> Tuple[bool, dict]:
    window = float(max(0.0, window))
    samples = int(max(1, samples))
    tmp_root = tempfile.mkdtemp(prefix="ads_end_verify_")
    try:
        scanner_src = EdgeScanner(ffmpeg_script, imagemagick_script, False, tmp_root,
                                  region_mode=region_mode, quality_threshold=quality_threshold)
        scanner_tgt = EdgeScanner(ffmpeg_script, imagemagick_script, False, tmp_root,
                                  region_mode=region_mode, quality_threshold=quality_threshold)
        if samples == 1:
            offsets = [min(window, 2.0)]
        else:
            offsets = [window * (i / (samples - 1)) for i in range(samples)]
        dists: List[int] = []
        valid = 0
        passed = 0
        for off in offsets:
            off = float(off)
            ts = max(0.0, float(source_duration) - max(1.0, off))
            tt = max(0.0, float(target_duration) - max(1.0, off))
            fs = scanner_src.get_features(source_path, ts)
            ft = scanner_tgt.get_features(target_path, tt)
            if fs is None or ft is None:
                continue
            d = scanner_src.distance(fs, ft, int(hash_threshold))
            if d is None:
                continue
            valid += 1
            dists.append(int(d))
            if int(d) <= int(hash_threshold):
                passed += 1
        ok = (valid >= 2 and passed >= max(2, int(math.ceil(valid * 0.6))))
        return ok, {"window": window, "samples": samples, "valid": valid, "passed": passed, "dists": dists, "thr": int(hash_threshold)}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

def _estimate_ratio_from_pairs(pairs: List[dict],
                               source_frame_info: Dict[int, dict],
                               target_frame_info: Dict[int, dict],
                               side: str,
                               n: int = 12) -> float:
    if len(pairs) < 3:
        return 1.0
    use = pairs[: min(len(pairs), n)] if side == "start" else pairs[max(0, len(pairs) - n):]
    pts = []
    for p in use:
        s = float(source_frame_info[p["main"]]["pts_s"])
        t = float(target_frame_info[p["twin"]]["pts_s"])
        pts.append((s, t))
    ratios = []
    for i in range(1, len(pts)):
        ds = pts[i][0] - pts[i - 1][0]
        dt = pts[i][1] - pts[i - 1][1]
        if ds > 0.2 and dt > 0.2:
            ratios.append(dt / ds)
    if not ratios:
        return 1.0
    ratios.sort()
    r = ratios[len(ratios) // 2]
    return float(clamp(r, 0.5, 2.0))


def scan_video_edges_deep(
    source_path: str,
    target_path: str,
    first_pair: dict,
    last_pair: dict,
    pairs_for_ratio: List[dict],
    source_frame_info: Dict[int, dict],
    target_frame_info: Dict[int, dict],
    source_duration: float,
    target_duration: float,
    ffmpeg_script: str,
    imagemagick_script: str,
    source_cut_borders: bool,
    target_cut_borders: bool,
    scan_step: float = 0.50,
    hash_threshold: int = 16,
    max_scan_back: float = 600.0,
    miss_limit_forward: int = 20,
    miss_limit_backward: int = 12,
    search_window: float = 1.0,
    search_step: float = 0.08,
    verify_offset: float = 0.18,
    region_mode: str = "multi",
    quality_threshold: float = 1.0,
    end_snap: float = 6.0,
):
    tmp_root = tempfile.mkdtemp(prefix="ads_deep_scan_")
    scanner_src = EdgeScanner(ffmpeg_script, imagemagick_script, source_cut_borders, tmp_root,
                              region_mode=region_mode, quality_threshold=quality_threshold)
    scanner_tgt = EdgeScanner(ffmpeg_script, imagemagick_script, target_cut_borders, tmp_root,
                              region_mode=region_mode, quality_threshold=quality_threshold)
    r_start = _estimate_ratio_from_pairs(pairs_for_ratio, source_frame_info, target_frame_info, "start")
    r_end = _estimate_ratio_from_pairs(pairs_for_ratio, source_frame_info, target_frame_info, "end")

    def best_target_time(s_time: float, t_pred: float, ratio: float) -> Optional[float]:
        fs = scanner_src.get_features(source_path, s_time)
        if fs is None:
            return None
        best_t = None
        best_d = None
        steps = int(math.ceil(search_window / max(1e-6, search_step)))
        offsets = [0.0]
        for i in range(1, steps + 1):
            offsets.append(i * search_step)
            offsets.append(-i * search_step)
        src_low = (float(fs.get("_meta", {}).get("max_q", 999.0)) < quality_threshold) if quality_threshold > 0 else False
        for dt in offsets:
            t = t_pred + dt
            if t < 0.0 or t > target_duration:
                continue
            ft = scanner_tgt.get_features(target_path, t)
            if ft is None:
                continue
            d0 = scanner_src.distance(fs, ft, hash_threshold)
            if d0 is None:
                continue
            tgt_low = (float(ft.get("_meta", {}).get("max_q", 999.0)) < quality_threshold) if quality_threshold > 0 else False
            if src_low and tgt_low:
                if d0 > (hash_threshold * 2):
                    continue
                score = d0
            else:
                if d0 > hash_threshold:
                    continue
                if verify_offset > 0.0:
                    fs2 = scanner_src.get_features(source_path, s_time + verify_offset)
                    ft2 = scanner_tgt.get_features(target_path, t + verify_offset * ratio)
                    if fs2 is None or ft2 is None:
                        continue
                    d1 = scanner_src.distance(fs2, ft2, hash_threshold)
                    if d1 is None or d1 > hash_threshold:
                        continue
                    score = d0 + d1
                else:
                    score = d0
            if best_d is None or score < best_d:
                best_d = score
                best_t = t
            if best_d is not None and best_d <= max(2, hash_threshold // 2):
                break
        return best_t

    src_anchor = float(source_frame_info[first_pair["main"]]["pts_s"])
    tgt_anchor = float(target_frame_info[first_pair["twin"]]["pts_s"])
    s_cur = src_anchor
    t_cur = tgt_anchor
    last_good_s = s_cur
    last_good_t = t_cur
    max_back_steps = int(max_scan_back / max(1e-6, scan_step))
    misses = 0
    for _ in range(max_back_steps):
        s_next = s_cur - scan_step
        t_pred = t_cur - r_start * scan_step
        if s_next < 0.0 or t_pred < 0.0:
            last_good_s = max(0.0, s_next)
            last_good_t = max(0.0, t_pred)
            break
        bt = best_target_time(s_next, t_pred, r_start)
        if bt is not None:
            s_cur = s_next
            t_cur = bt
            last_good_s = s_cur
            last_good_t = t_cur
            misses = 0
        else:
            misses += 1
            s_cur = s_next
            t_cur = t_pred
            if misses >= miss_limit_backward:
                break
    final_src_start = max(0.0, last_good_s)
    final_tgt_start = max(0.0, last_good_t)

    src_anchor = float(source_frame_info[last_pair["main"]]["pts_s"])
    tgt_anchor = float(target_frame_info[last_pair["twin"]]["pts_s"])
    s_cur = src_anchor
    t_cur = tgt_anchor
    last_good_s = s_cur
    last_good_t = t_cur
    misses = 0
    while True:
        s_next = s_cur + scan_step
        t_pred = t_cur + r_end * scan_step
        if s_next > source_duration or t_pred > target_duration:
            break
        bt = best_target_time(s_next, t_pred, r_end)
        if bt is not None:
            s_cur = s_next
            t_cur = bt
            last_good_s = s_cur
            last_good_t = t_cur
            misses = 0
        else:
            misses += 1
            s_cur = s_next
            t_cur = t_pred
            if misses >= miss_limit_forward:
                break
    final_src_end = min(source_duration, last_good_s)
    final_tgt_end = min(target_duration, last_good_t)

    try:
        tail_src = float(source_duration) - float(final_src_end)
        tail_tgt = float(target_duration) - float(final_tgt_end)
        max_tail = max(tail_src, tail_tgt)
        if max_tail > max(10.0, float(end_snap) * 2.0):
            sample_offsets = [50.0, 35.0, 25.0, 15.0, 10.0, 6.0]
            probe_offsets = [0.0, 0.25, -0.25, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0, 3.0, -3.0]
            ok = 0
            used = 0
            probe_threshold = int(max(hash_threshold + 10, hash_threshold * 2))
            for off in sample_offsets:
                t = float(target_duration) - float(off)
                if t <= float(final_tgt_end) + 0.2:
                    continue
                if t <= 0.5 or t > float(target_duration):
                    continue
                ft = scanner_tgt.get_features(target_path, t)
                if ft is None:
                    continue
                if quality_threshold > 0:
                    if float(ft.get("_meta", {}).get("max_q", 0.0)) < float(quality_threshold):
                        continue
                s_pred = float(final_src_end) + (t - float(final_tgt_end)) / max(1e-6, float(r_end))
                if s_pred < 0.0 or s_pred > float(source_duration):
                    continue
                used += 1
                best = None
                for ds in probe_offsets:
                    s_try = s_pred + float(ds)
                    if s_try < 0.0 or s_try > float(source_duration):
                        continue
                    fs = scanner_src.get_features(source_path, s_try)
                    if fs is None:
                        continue
                    if quality_threshold > 0:
                        if float(fs.get("_meta", {}).get("max_q", 0.0)) < float(quality_threshold):
                            continue
                    d = scanner_src.distance(fs, ft, probe_threshold)
                    if d is None:
                        continue
                    if best is None or d < best:
                        best = d
                if best is not None and int(best) <= probe_threshold:
                    ok += 1
            if used >= 2 and ok >= 2:
                print(f"🛡️ Smart end-probe: tail seems matching (ok={ok}/{used}). Keeping full ending.")
                final_src_end = float(source_duration)
                final_tgt_end = float(target_duration)
    except Exception:
        pass

    if (source_duration - final_src_end) < float(end_snap):
        final_src_end = source_duration
    if (target_duration - final_tgt_end) < float(end_snap):
        final_tgt_end = target_duration

    shutil.rmtree(tmp_root, ignore_errors=True)
    return final_src_start, final_src_end, final_tgt_start, final_tgt_end, r_start, r_end


# --------------------------
# Refine boundaries
# --------------------------

@dataclass
class FrameHasher:
    ffmpeg_script: str
    imagemagick_script: str
    cut_borders: bool
    temp_dir: str

    def get_hash(self, video_path: str, timestamp: float) -> Optional[Tuple[object, object]]:
        ts = max(0.0, float(timestamp))
        safe_name = f"{abs(hash((video_path, round(ts, 3)))):x}"
        img_path = os.path.join(self.temp_dir, f"frame_{safe_name}.jpg")
        if not os.path.exists(img_path):
            cmd = f'{self.ffmpeg_script} -y -ss {ts:.3f} -i "{video_path}" -vframes 1 -q:v 2 "{img_path}"'
            ffmpeg_run(cmd, quiet=True, purpose='preview_frame')
            if self.cut_borders and os.path.exists(img_path):
                cmd2 = (
                    f'{self.imagemagick_script} mogrify -fuzz 4% -define trim:percent-background=0% '
                    f'-trim +repage "{img_path}"'
                )
                run_cmd(cmd2, quiet=True)
        if not os.path.exists(img_path):
            return None
        try:
            img = Image.open(img_path)
            return (imagehash.phash(img), imagehash.dhash(img))
        except Exception:
            return None


def hash_distance(hA: Tuple[object, object], hB: Tuple[object, object]) -> int:
    return int((hA[0] - hB[0]) + (hA[1] - hB[1]))


def refine_one_segment_alignment_v2(
    source_path: str,
    target_path: str,
    seg: dict,
    hasher_source: FrameHasher,
    hasher_target: FrameHasher,
    max_hash_distance: int,
    coarse_step: float,
    refine_iters: int,
    end_search_window: float,
    end_search_step: float
) -> Optional[dict]:
    D = float(seg["insert_duration"])
    s0 = float(seg["source_start"])
    s1 = float(seg["source_end"])
    t0 = float(seg["target_start"])
    t1 = float(seg["target_end"])
    T = min(s1 - s0, t1 - t0)
    if T <= 0.7 or D <= 0.7:
        return None

    def dist_pair(ts_src: float, ts_tgt: float) -> Optional[int]:
        hs = hasher_source.get_hash(source_path, ts_src)
        ht = hasher_target.get_hash(target_path, ts_tgt)
        if hs is None or ht is None:
            return None
        return hash_distance(hs, ht)

    def classify(t: float) -> Optional[int]:
        offsets = [0.0, 0.12]
        dA_list = []
        dB_list = []
        for off in offsets:
            tt = clamp(t + off, 0.0, T)
            if seg["video"] == "source":
                dA = dist_pair(s0 + tt, t0 + tt)
                dB = dist_pair(s0 + tt + D, t0 + tt)
            else:
                dA = dist_pair(s0 + tt, t0 + tt)
                dB = dist_pair(s0 + tt, t0 + tt + D)
            if dA is not None:
                dA_list.append(dA)
            if dB is not None:
                dB_list.append(dB)
        if not dA_list or not dB_list:
            return None
        dA = sorted(dA_list)[len(dA_list) // 2]
        dB = sorted(dB_list)[len(dB_list) // 2]
        goodA = dA <= max_hash_distance
        goodB = dB <= max_hash_distance
        if goodA and not goodB:
            return 0
        if goodB and not goodA:
            return 1
        return 0 if dA < dB else 1 if dB < dA else None

    samples = []
    steps = int(max(2, math.ceil(T / max(0.05, coarse_step))))
    for i in range(steps + 1):
        tt = min(T, i * coarse_step)
        c = classify(tt)
        if c in (0, 1):
            samples.append((tt, c))
    last_before = None
    first_after = None
    seen_before = False
    for tt, c in samples:
        if c == 0:
            seen_before = True
            if first_after is None:
                last_before = tt
        elif c == 1 and first_after is None and seen_before:
            first_after = tt
            break
    if last_before is None:
        last_before = 0.0
    if first_after is None:
        first_after = T
    lo = last_before
    hi = first_after
    for _ in range(refine_iters):
        mid = (lo + hi) / 2.0
        c = classify(mid)
        if c is None:
            break
        if c == 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.02:
            break
    boundary_t = clamp((lo + hi) / 2.0, 0.0, T)
    if seg["video"] == "source":
        start_ref = clamp(s0 + boundary_t, s0, max(s0, s1 - D))
        anchor_other = t0 + boundary_t
    else:
        start_ref = clamp(t0 + boundary_t, t0, max(t0, t1 - D))
        anchor_other = s0 + boundary_t
    end_ref0 = start_ref + D
    eval_offsets = [0.0, 0.4, 0.8]
    best_end = end_ref0
    best_score = float("inf")
    end_max = s1 if seg["video"] == "source" else t1
    a = max(start_ref + 0.2, end_ref0 - end_search_window)
    b = min(end_max, end_ref0 + end_search_window)
    cur = a
    while cur <= b + 1e-6:
        score = 0.0
        ok = True
        for off in eval_offsets:
            if seg["video"] == "source":
                d = dist_pair(cur + off, anchor_other + off)
            else:
                d = dist_pair(anchor_other + off, cur + off)
            if d is None:
                ok = False
                break
            score += d
        if ok and score < best_score:
            best_score = score
            best_end = cur
        cur += end_search_step
    start_ref = float(start_ref)
    end_ref = float(best_end)
    if end_ref <= start_ref + 0.3:
        return None
    seg2 = seg.copy()
    seg2["cut_start_abs"] = start_ref
    seg2["cut_end_abs"] = end_ref
    seg2["insert_duration"] = float(end_ref - start_ref)
    return seg2


def refine_cut_boundaries_alignment_v2(
    source_path: str,
    target_path: str,
    cut_segments: List[dict],
    source_cut_borders: bool,
    target_cut_borders: bool,
    max_hash_distance: int,
    coarse_step: float,
    refine_iters: int,
    end_search_window: float,
    end_search_step: float,
    ffmpeg_script: str,
    imagemagick_script: str
) -> List[dict]:
    refined = []
    tmp_root = tempfile.mkdtemp(prefix="ads_refine_")
    try:
        hs = FrameHasher(ffmpeg_script, imagemagick_script, source_cut_borders, tmp_root)
        ht = FrameHasher(ffmpeg_script, imagemagick_script, target_cut_borders, tmp_root)
        for seg in cut_segments:
            r = refine_one_segment_alignment_v2(
                source_path, target_path, seg,
                hs, ht,
                max_hash_distance=max_hash_distance,
                coarse_step=coarse_step,
                refine_iters=refine_iters,
                end_search_window=end_search_window,
                end_search_step=end_search_step
            )
            refined.append(r if r is not None else seg)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    for s in refined:
        if "cut_start_abs" not in s:
            if s["video"] == "source":
                s["cut_start_abs"] = float(s["source_start"])
            else:
                s["cut_start_abs"] = float(s["target_start"])
            s["cut_end_abs"] = float(s["cut_start_abs"]) + float(s["insert_duration"])
    return refined


# --------------------------
# Cutting inserts
# --------------------------

def cut_large_inserts(
    video_path: str,
    cut_segments_rel: List[dict],
    output_path: str,
    workdir: str,
    ffmpeg_script: str = "ffmpeg",
    cut_mode: str = "copy",
    cut_pad: float = 0.0,
    keep_audio: bool = True,
    cut_crf: int = 18,
    cut_preset: str = "veryfast",
):
    if not cut_segments_rel:
        shutil.copy2(video_path, output_path)
        return output_path
    ensure_dir(os.path.dirname(output_path) or ".")
    duration = get_duration(video_path)
    intervals: List[Tuple[float, float]] = []
    for seg in cut_segments_rel:
        st = float(seg["cut_start_rel"])
        en = st + float(seg["insert_duration"])
        st2 = max(0.0, st - cut_pad)
        en2 = min(duration, en + cut_pad)
        if en2 - st2 >= 0.25:
            intervals.append((st2, en2))
    if not intervals:
        shutil.copy2(video_path, output_path)
        return output_path
    intervals.sort(key=lambda x: x[0])
    merged: List[List[float]] = []
    for a, b in intervals:
        if not merged or a > merged[-1][1] + 0.05:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    keep_segments: List[Tuple[float, float]] = []
    last = 0.0
    for a, b in merged:
        if a > last + 0.05:
            keep_segments.append((last, a))
        last = b
    if last < duration - 0.05:
        keep_segments.append((last, duration))
    if not keep_segments:
        raise RuntimeError("Cut removed entire video (keep_segments empty)")
    tmp_dir = ensure_dir(os.path.join(workdir, "_tmp_cut"))
    concat_list = os.path.join(tmp_dir, "concat_list.txt")
    if cut_mode == "copy":
        temp_files = []
        ext = os.path.splitext(output_path)[1].lower() or ".mp4"
        for idx, (a, b) in enumerate(keep_segments):
            tmp = os.path.join(tmp_dir, f"keep_{idx:03d}{ext}")
            temp_files.append(tmp)
            cmd = f'{ffmpeg_script} -y -i "{video_path}" -ss {a:.3f} -to {b:.3f} -c copy "{tmp}"'
            ffmpeg_run(cmd, quiet=True, purpose='preview_frame')
        with open(concat_list, "w", encoding="utf-8") as f:
            for tmp in temp_files:
                f.write(f"file '{_ff_concat_path(tmp)}'\n")
        cmd = f'{ffmpeg_script} -y -probesize 100M -analyzeduration 100M -f concat -safe 0 -i "{concat_list}" -c copy "{output_path}"'
        ffmpeg_run(cmd, quiet=False, purpose='ffmpeg')
        return output_path
    want_audio = keep_audio and has_audio_stream(video_path)
    n = len(keep_segments)
    parts = []
    for i, (a, b) in enumerate(keep_segments):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if want_audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    if want_audio:
        concat_in = "".join([f"[v{i}][a{i}]" for i in range(n)])
        parts.append(f"{concat_in}concat=n={n}:v=1:a=1[v][a]")
    else:
        concat_in = "".join([f"[v{i}]" for i in range(n)])
        parts.append(f"{concat_in}concat=n={n}:v=1:a=0[v]")
    fc = ";".join(parts)
    cmd = f'{ffmpeg_script} -y -i "{video_path}" -filter_complex "{fc}" -map "[v]"'
    cmd += f' -c:v libx264 -preset {cut_preset} -crf {cut_crf}'
    if want_audio:
        cmd += ' -map "[a]" -c:a aac -b:a 192k'
    else:
        cmd += ' -an'
    cmd += f' "{output_path}"'
    ffmpeg_run(cmd, quiet=False, purpose='ffmpeg')
    return output_path


# --------------------------
# Audio: rubberband timemap
# --------------------------

def create_rubberband_timemap_simple(twins: List[dict], source_frame_info: Dict[int, dict], target_frame_info: Dict[int, dict],
                                     source_audio_hz: int, output_file: str):
    ensure_dir(os.path.dirname(output_file) or ".")
    timecodes = []
    for pair in twins:
        src_time = float(source_frame_info[pair["main"]]["pts_s"])
        tgt_time = float(target_frame_info[pair["twin"]]["pts_s"])
        src_sample = int(src_time * source_audio_hz)
        tgt_sample = int(tgt_time * source_audio_hz)
        timecodes.append([src_sample, tgt_sample])
    with open(output_file, "w", encoding="utf-8") as f:
        for a, b in timecodes:
            f.write(f"{a} {b}\n")
    return timecodes


def choose_audio_work_format(sample_rate: int) -> str:
    return "opus" if sample_rate in (48000, 24000, 16000, 12000, 8000) else "wav"


def extract_audio_for_rubberband(video_path: str, out_audio: str, sample_rate: int,
                                 ffmpeg_script: str = "ffmpeg") -> bool:
    ensure_dir(os.path.dirname(out_audio) or ".")
    ext = os.path.splitext(out_audio)[1].lower()
    if ext == ".opus":
        cmd = (
            f'{ffmpeg_script} -y -i "{video_path}" -vn -ar {sample_rate} '
            f'-c:a libopus -b:a 320k "{out_audio}"'
        )
    else:
        cmd = (
            f'{ffmpeg_script} -y -i "{video_path}" -vn -ar {sample_rate} '
            f'-c:a pcm_s16le "{out_audio}"'
        )
    res = ffmpeg_run(cmd, quiet=False, capture_output=True, purpose='scene_detect')
    if os.path.exists(out_audio) and os.path.getsize(out_audio) > 1000:
        return True
    if res.stderr:
        print(res.stderr[:800])
    return False


def apply_audio_stretch_ffmpeg(input_audio: str, output_audio: str, tempo_ratio: float,
                               ffmpeg_script: str = "ffmpeg") -> bool:
    ensure_dir(os.path.dirname(output_audio) or ".")
    filters = []
    current = float(tempo_ratio)
    while current < 0.5:
        filters.append("atempo=0.5")
        current /= 0.5
    while current > 2.0:
        filters.append("atempo=2.0")
        current /= 2.0
    filters.append(f"atempo={current:.6f}")
    chain = ",".join(filters)
    ext = os.path.splitext(output_audio)[1].lower()
    if ext == ".opus":
        cmd = f'{ffmpeg_script} -y -i "{input_audio}" -filter:a "{chain}" -c:a libopus -b:a 320k "{output_audio}"'
    else:
        cmd = f'{ffmpeg_script} -y -i "{input_audio}" -filter:a "{chain}" "{output_audio}"'
    res = ffmpeg_run(cmd, quiet=False, capture_output=True, purpose='scene_detect')
    if os.path.exists(output_audio) and os.path.getsize(output_audio) > 1000:
        return True
    if res.stderr:
        print(res.stderr[:800])
    return False


# --------------------------
# Trim edges
# --------------------------

def trim_video_edges_return(video_path: str, start_time: float, end_time: float, output_path: str,
                            trim_threshold: float, ffmpeg_script: str = "ffmpeg") -> Tuple[str, float, float]:
    ensure_dir(os.path.dirname(output_path) or ".")
    duration = get_duration(video_path)
    start_time = max(0.0, float(start_time))
    end_time = min(duration, float(end_time))
    if start_time < trim_threshold:
        used_start = 0.0
    else:
        used_start = start_time
    tail = duration - end_time
    if tail < trim_threshold:
        used_end = duration
    else:
        used_end = end_time
    if used_start <= 0.0 and abs(used_end - duration) < 1e-3:
        shutil.copy2(video_path, output_path)
        return output_path, used_start, used_end
    cmd = f'{ffmpeg_script} -y -i "{video_path}" -ss {used_start:.3f} -to {used_end:.3f} -c copy "{output_path}"'
    ffmpeg_run(cmd, quiet=True, purpose='preview_frame')
    return output_path, used_start, used_end


# --------------------------
# Scan phase
# --------------------------

def perform_scan(args) -> dict:
    ffmpeg_script = args.ffmpeg
    imagemagick_script = args.imagemagick
    workdir = os.path.abspath(args.workdir)
    ensure_dir(workdir)
    progress_emit('scan', 0.02, 'Подготовка…', step='start')
    source_path = args.source_path
    target_path = args.target_path
    progress_emit('scan', 0.03, 'Читаю метаданные (ffprobe)…', step='ffprobe')
    print("Getting audio hz via ffprobe...")
    source_audio_hz = get_audio_hz(source_path)
    print("Getting video duration via ffprobe...")
    source_duration = get_duration(source_path)
    print("Getting video fps via ffprobe...")
    source_fps = get_fps(source_path)
    print("Getting video tbn via ffprobe...")
    source_tbn = get_tbn(source_path)
    source_pos_per_frame = source_tbn / source_fps
    print("Getting video duration via ffprobe...")
    target_duration = get_duration(target_path)
    print("Getting video fps via ffprobe...")
    target_fps = get_fps(target_path)
    print("Getting video tbn via ffprobe...")
    target_tbn = get_tbn(target_path)
    target_pos_per_frame = target_tbn / target_fps
    source_audio_samples_per_frame = source_audio_hz / source_fps
    print("")
    print("=" * 80)
    print(f"Source: {os.path.basename(source_path)} - FPS: {source_fps:.3f}, Duration: {source_duration:.1f}s")
    print(f"Target: {os.path.basename(target_path)} - FPS: {target_fps:.3f}, Duration: {target_duration:.1f}s")
    print(f"Разница длительностей: {abs(source_duration - target_duration):.1f}s")
    print(f"Scene threshold (-fdp): {args.frame_diff_percentage}%")
    print(f"Trim threshold (-tt): {args.trim_threshold}s")
    print(f"Deep Scan edges: {'OFF' if args.no_deep_scan_edges else 'ON (tracking)'}")
    print(f"Cut mode: {args.cut_mode}, pad={args.cut_pad}s")
    print("=" * 80 + "\n")
    frames_root = ensure_dir(os.path.join(workdir, "frames"))
    source_frames_folder = ensure_dir(os.path.join(frames_root, "SOURCE_FRAMES"))
    target_frames_folder = ensure_dir(os.path.join(frames_root, "TARGET_FRAMES"))
    print("Getting video new scene frame information via ffmpeg...")
    source_frames = capture_frame_info(
        video_path=source_path,
        output_folder=source_frames_folder,
        cut_borders=args.source_cut_borders,
        frame_diff=args.frame_diff_percentage,
        video_tbn=source_tbn,
        video_fps=source_fps,
        video_pos_per_frame=source_pos_per_frame,
        audio_samples_per_frame=source_audio_samples_per_frame,
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        keep_frame_dumps=args.keep_frame_dumps,
    )
    progress_emit('scan', 0.30, 'Детекция сцен завершена: SOURCE', step='scene_source')
    print("Getting video new scene frame information via ffmpeg...")
    target_frames = capture_frame_info(
        video_path=target_path,
        output_folder=target_frames_folder,
        cut_borders=args.target_cut_borders,
        frame_diff=args.frame_diff_percentage,
        video_tbn=target_tbn,
        video_fps=target_fps,
        video_pos_per_frame=target_pos_per_frame,
        audio_samples_per_frame=0,
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        keep_frame_dumps=args.keep_frame_dumps,
    )
    progress_emit('scan', 0.52, 'Детекция сцен завершена: TARGET', step='scene_target')
    source_frame_info = {f["scene_frame_index"]: f for f in source_frames}
    target_frame_info = {f["scene_frame_index"]: f for f in target_frames}
    print("\n🔍 Поиск совпадающих кадров...")
    if len(source_frames) == 0 or len(target_frames) == 0:
        raise RuntimeError("No scene frames for matching (try smaller -fdp / enable -scb/-tcb)")
    match_thr = getattr(args, "match_hash_threshold", None)
    match_min = getattr(args, "match_min_count", 80) if match_thr is not None else 0
    if len(target_frames) < len(source_frames):
        main_frames, bro_frames, rev = target_frames, source_frames, True
    else:
        main_frames, bro_frames, rev = source_frames, target_frames, False
    all_twins = find_twin_frames(main_frames, bro_frames, reverse_main_and_twin=rev, max_distance=match_thr)
    deep_thr = getattr(args, "deep_hash_threshold", None)
    if match_thr is not None and deep_thr is not None and match_thr < deep_thr and len(all_twins) < match_min:
        best = all_twins
        best_thr = match_thr
        for thr in range(match_thr + 2, deep_thr + 1, 2):
            cand = find_twin_frames(main_frames, bro_frames, reverse_main_and_twin=rev, max_distance=thr)
            if len(cand) >= len(best):
                best = cand
                best_thr = thr
            if len(best) >= match_min:
                break
        all_twins = best
        if best_thr != match_thr:
            print(f"⚠️ Совпадений мало → увеличил match-hash-threshold до {best_thr} (совпадений: {len(all_twins)})")
    print(f"✅ Найдено {len(all_twins)} совпадений\n")
    progress_emit('scan', 0.60, f'Совпадения найдены: {len(all_twins)}', step='matches')
    if len(all_twins) < 3:
        raise RuntimeError("Too few matches")
    if args.auto_sync:
        start_idx, end_idx = auto_find_sync_points(all_twins, source_frame_info, target_frame_info)
    else:
        start_idx = int(input(f"Enter start frame pair index 0-{len(all_twins)-1}: "))
        end_idx = int(input(f"Enter end frame pair index 0-{len(all_twins)-1}: "))
    processed_twins = all_twins[start_idx:end_idx + 1]
    if len(processed_twins) < 3:
        raise RuntimeError("Too few matches after sync range")
    src_trim_start0 = float(source_frame_info[processed_twins[0]["main"]]["pts_s"])
    src_trim_end0 = float(source_frame_info[processed_twins[-1]["main"]]["pts_s"])
    tgt_trim_start0 = float(target_frame_info[processed_twins[0]["twin"]]["pts_s"])
    tgt_trim_end0 = float(target_frame_info[processed_twins[-1]["twin"]]["pts_s"])
    end_override = None
    if not args.no_deep_scan_edges:
        print("\n🔬 Deep Scan (tracking) границ...")
        src_trim_start, src_trim_end, tgt_trim_start, tgt_trim_end, r_start, r_end = scan_video_edges_deep(
            source_path=source_path,
            target_path=target_path,
            first_pair=processed_twins[0],
            last_pair=processed_twins[-1],
            pairs_for_ratio=processed_twins,
            source_frame_info=source_frame_info,
            target_frame_info=target_frame_info,
            source_duration=source_duration,
            target_duration=target_duration,
            ffmpeg_script=ffmpeg_script,
            imagemagick_script=imagemagick_script,
            source_cut_borders=args.source_cut_borders,
            target_cut_borders=args.target_cut_borders,
            scan_step=float(args.deep_scan_step),
            hash_threshold=int(args.deep_hash_threshold),
            max_scan_back=float(args.deep_scan_back),
            miss_limit_forward=int(args.deep_miss_forward),
            miss_limit_backward=int(args.deep_miss_backward),
            search_window=float(args.deep_search_window),
            search_step=float(args.deep_search_step),
            verify_offset=float(args.deep_verify_offset),
            region_mode=str(args.deep_region_mode),
            quality_threshold=float(args.deep_quality_threshold),
            end_snap=float(getattr(args, "deep_end_snap", 6.0)),
        )
    else:
        src_trim_start, src_trim_end, tgt_trim_start, tgt_trim_end = src_trim_start0, src_trim_end0, tgt_trim_start0, tgt_trim_end0
        r_start, r_end = 1.0, 1.0
    try:
        dur_diff = abs(float(source_duration) - float(target_duration))
        if float(getattr(args, 'keep_end_dur_tol', 0.0) or 0.0) > 0 and dur_diff <= float(args.keep_end_dur_tol):
            tail_src = max(0.0, float(source_duration) - float(src_trim_end))
            tail_tgt = max(0.0, float(target_duration) - float(tgt_trim_end))
            max_tail = float(getattr(args, 'keep_end_max_tail', 0.0) or 0.0)
            if (max_tail <= 0.0) or (max(tail_src, tail_tgt) <= max_tail):
                if tail_src > 0.05 or tail_tgt > 0.05:
                    print(f"🛡️ Keep-end guard: durations close (Δ={dur_diff:.3f}s). Extending trim_end to full duration.")
                src_trim_end = float(source_duration)
                tgt_trim_end = float(target_duration)
    except Exception:
        pass
    try:
        same_tail_max = float(getattr(args, 'keep_end_same_tail', 0.0) or 0.0)
        same_tail_tol = float(getattr(args, 'keep_end_same_tail_tol', 0.0) or 0.0)
        if same_tail_max > 0.0:
            tail_src = max(0.0, float(source_duration) - float(src_trim_end))
            tail_tgt = max(0.0, float(target_duration) - float(tgt_trim_end))
            if (tail_src > 0.05 or tail_tgt > 0.05) and max(tail_src, tail_tgt) <= same_tail_max and abs(tail_src - tail_tgt) <= max(0.0, same_tail_tol):
                verify_window = float(getattr(args, 'keep_end_verify_window', same_tail_max) or same_tail_max)
                verify_samples = int(getattr(args, 'keep_end_verify_samples', 4) or 4)
                verify_thr = int(getattr(args, 'keep_end_verify_hash_threshold', 0) or 0)
                if verify_thr <= 0:
                    verify_thr = int(getattr(args, 'deep_hash_threshold', 16) or 16)
                ok_verify = True
                verify_details = None
                if verify_window > 0.0 and verify_samples > 0:
                    ok_verify, verify_details = verify_end_similarity(
                        source_path=source_path,
                        target_path=target_path,
                        source_duration=float(source_duration),
                        target_duration=float(target_duration),
                        ffmpeg_script=args.ffmpeg,
                        imagemagick_script=args.imagemagick,
                        region_mode=str(getattr(args, 'deep_region_mode', 'multi') or 'multi'),
                        quality_threshold=float(getattr(args, 'deep_quality_threshold', 1.0) or 1.0),
                        hash_threshold=int(verify_thr),
                        window=float(min(verify_window, same_tail_max)),
                        samples=int(verify_samples),
                    )
                if ok_verify:
                    print(f"🛡️ Keep-end guard: same tail trims within tol={same_tail_tol:.2f}s. Extending trim_end.")
                    end_override = {
                        "mode": "same_tail",
                        "tail_src": float(tail_src),
                        "tail_tgt": float(tail_tgt),
                        "max": float(same_tail_max),
                        "tol": float(same_tail_tol),
                        "verify": verify_details,
                    }
                    src_trim_end = float(source_duration)
                    tgt_trim_end = float(target_duration)
    except Exception:
        pass
    progress_emit('scan', 0.70, 'Границы (edges) определены', step='edges')
    print("\n" + "=" * 80)
    print("📐 Обрезка несинхронизированных частей")
    print("=" * 80)
    print(f"Source: {src_trim_start:.2f}s - {src_trim_end:.2f}s")
    print(f"Target: {tgt_trim_start:.2f}s - {tgt_trim_end:.2f}s")
    print(f"Start diff: {abs(src_trim_start - tgt_trim_start):.3f}s")
    print("=" * 80 + "\n")
    print("=" * 80)
    print("🔍 Анализ середины видео (детекция вставок)")
    print("=" * 80)
    all_jumps, cut_segments, stretch_segments = detect_time_jumps_hybrid(
        processed_twins, source_frame_info, target_frame_info,
        small_threshold=args.small_threshold,
        large_threshold=args.large_threshold,
        min_insert_cut=float(getattr(args, "min_insert_cut", 3.0) or 3.0),
        prefer_longer_cuts=bool(getattr(args, "prefer_longer_cuts", True)),
        duration_bias=float(source_duration - target_duration),
        min_insert_cut_shorter=(None if getattr(args, "min_insert_cut_shorter", None) is None else float(args.min_insert_cut_shorter)),
    )
    micro_verify_downgraded: List[dict] = []
    try:
        if cut_segments and bool(getattr(args, "micro_verify", True)):
            mv_max_len = float(getattr(args, "micro_verify_max_len", 6.0) or 6.0)
            mv_step = float(getattr(args, "micro_verify_step", 0.10) or 0.10)
            mv_jitters = str(getattr(args, "micro_verify_jitters", "0,0.05") or "0,0.05")
            mv_win = float(getattr(args, "micro_verify_window", 0.60) or 0.60)
            mv_thr = int(getattr(args, "micro_verify_hash_threshold", 0) or 0)
            if mv_thr <= 0:
                mv_thr = int(getattr(args, "match_hash_threshold", 10) or 10)
            mv_keep_max = float(getattr(args, "micro_verify_keep_max_match", 0.20) or 0.20)
            mv_min_gap = float(getattr(args, "micro_verify_min_gap", 0.60) or 0.60)
            mv_tmp = ensure_dir(os.path.join(workdir, "_microverify"))
            new_cuts: List[dict] = []
            for seg in cut_segments:
                try:
                    ins = float(seg.get("insert_duration", 0.0) or 0.0)
                    diff = float(seg.get("difference", ins) or ins)
                except Exception:
                    ins = 0.0
                    diff = 0.0
                if ins > 0.0 and ins <= mv_max_len and diff < float(args.large_threshold):
                    mv = micro_verify_cut_candidate(
                        seg,
                        source_path=source_path,
                        target_path=target_path,
                        source_cut_borders=bool(args.source_cut_borders),
                        target_cut_borders=bool(args.target_cut_borders),
                        ffmpeg_script=ffmpeg_script,
                        imagemagick_script=imagemagick_script,
                        temp_dir=mv_tmp,
                        region_mode=str(getattr(args, "deep_region_mode", "multi") or "multi"),
                        quality_threshold=float(getattr(args, "deep_quality_threshold", 1.0) or 1.0),
                        hash_threshold=int(mv_thr),
                        sample_step=float(mv_step),
                        sample_jitters=mv_jitters,
                        search_window=float(mv_win),
                        keep_cut_max_match_rate=float(mv_keep_max),
                        min_unmatched_run_s=float(mv_min_gap),
                    )
                    seg["micro_verify"] = mv
                    if mv.get("decision") == "downgrade_to_stretch":
                        seg["action"] = "stretch"
                        seg["downgraded_from_cut"] = True
                        seg["downgrade_reason"] = str(mv.get("reason") or "micro_verify")
                        micro_verify_downgraded.append(seg)
                        stretch_segments.append(seg)
                        continue
                new_cuts.append(seg)
            cut_segments = new_cuts
    except Exception:
        micro_verify_downgraded = []
    print("\n📊 Результаты анализа:")
    print(f"   Всего скачков: {len(all_jumps)}")
    print(f"   Вставок CUT: {len(cut_segments)}")
    print(f"   Сегментов STRETCH: {len(stretch_segments)}\n")
    progress_emit('scan', 0.78, f'Анализ вставок: CUT={len(cut_segments)}, STRETCH={len(stretch_segments)}', step='analyze')
    cut_segments_refined: List[dict] = []
    if cut_segments and args.use_precise_scene_detect and args.refine_method == "alignment":
        print("=" * 80)
        print("🔬 Уточнение границ рекламы (alignment)")
        print("=" * 80 + "\n")
        cut_segments_refined = refine_cut_boundaries_alignment_v2(
            source_path, target_path, cut_segments,
            source_cut_borders=args.source_cut_borders,
            target_cut_borders=args.target_cut_borders,
            max_hash_distance=args.refine_hash_threshold,
            coarse_step=args.refine_coarse_step,
            refine_iters=args.refine_iters,
            end_search_window=max(0.8, float(args.search_window)),
            end_search_step=0.10,
            ffmpeg_script=ffmpeg_script,
            imagemagick_script=imagemagick_script,
        )
    else:
        for s in cut_segments:
            s2 = s.copy()
            if s2["video"] == "source":
                s2["cut_start_abs"] = float(s2["source_start"])
            else:
                s2["cut_start_abs"] = float(s2["target_start"])
            s2["cut_end_abs"] = float(s2["cut_start_abs"]) + float(s2["insert_duration"])
            cut_segments_refined.append(s2)
    ignored_short_cuts: List[dict] = []
    try:
        min_cut_len = float(getattr(args, 'min_cut_len', 0.0) or 0.0)
    except Exception:
        min_cut_len = 0.0
    if cut_segments_refined and min_cut_len > 0.0:
        kept: List[dict] = []
        for s in cut_segments_refined:
            try:
                dur = float(s.get('insert_duration', 0.0) or 0.0)
            except Exception:
                dur = 0.0
            try:
                diff_raw = float(s.get('difference_raw', s.get('difference', dur)) or dur)
            except Exception:
                diff_raw = dur
            if dur >= min_cut_len or diff_raw >= float(args.large_threshold):
                kept.append(s)
            else:
                s_ig = dict(s)
                s_ig['ignored_reason'] = f'short<{min_cut_len:.2f}s'
                ignored_short_cuts.append(s_ig)
        cut_segments_refined = kept
    progress_emit('scan', 0.86, 'Уточнение границ завершено', step='refine')
    previews_root = ensure_dir(os.path.join(workdir, "previews"))
    edges_dir = ensure_dir(os.path.join(previews_root, "edges"))
    cuts_dir = ensure_dir(os.path.join(previews_root, "cuts"))
    edge_previews = {
        "source": save_edge_previews(source_path, src_trim_start, src_trim_end, source_duration,
                                     edges_dir, "SOURCE", ffmpeg_script),
        "target": save_edge_previews(target_path, tgt_trim_start, tgt_trim_end, target_duration,
                                     edges_dir, "TARGET", ffmpeg_script),
    }
    edges_for_report: Dict[str, Dict[str, str]] = {}
    src_edges = (edge_previews.get("source") or {}) if isinstance(edge_previews, dict) else {}
    def _edge_entry(key: str, label: str):
        p = src_edges.get(key)
        if p:
            edges_for_report[label] = {"clip_path": str(p), "label": label}
    _edge_entry("start_kept", "start")
    _edge_entry("end_kept", "end")
    _edge_entry("start_removed", "start_removed")
    _edge_entry("end_removed", "end_removed")
    cut_previews: List[dict] = []
    for i, seg in enumerate(cut_segments_refined, 1):
        vpath = source_path if seg["video"] == "source" else target_path
        vdur = source_duration if seg["video"] == "source" else target_duration
        st = float(seg["cut_start_abs"])
        en = float(seg["cut_end_abs"])
        out_dir = ensure_dir(os.path.join(cuts_dir, f"{i:03d}"))
        cut_file = os.path.join(out_dir, "CUT_SEGMENT.mp4")
        before_file = os.path.join(out_dir, "BEFORE_CUT.mp4")
        after_file = os.path.join(out_dir, "AFTER_CUT.mp4")
        frames_dir = ensure_dir(os.path.join(out_dir, "frames"))
        bef0, bef1 = max(0.0, st - 3.0), st
        aft0, aft1 = en, min(vdur, en + 3.0)
        if (bef1 - bef0) < 0.05:
            bef0, bef1 = 0.0, min(vdur, 3.0)
        if (aft1 - aft0) < 0.05:
            aft0, aft1 = max(0.0, vdur - 3.0), vdur
        save_cut_segment_video(vpath, st, en, cut_file, ffmpeg_script)
        save_cut_segment_video(vpath, bef0, bef1, before_file, ffmpeg_script)
        save_cut_segment_video(vpath, aft0, aft1, after_file, ffmpeg_script)
        save_preview_frames(vpath, st, en, frames_dir, num_frames=8, ffmpeg_script=ffmpeg_script)
        cut_previews.append({
            "index": i,
            "video": seg["video"],
            "cut_start_abs": st,
            "cut_end_abs": en,
            "duration": float(en - st),
            "dir": out_dir,
            "cut": cut_file,
            "before": before_file,
            "after": after_file,
            "frames_dir": frames_dir,
        })
    for _cp in cut_previews:
        _cp.setdefault('cut_path', _cp.get('cut'))
        _cp.setdefault('before_path', _cp.get('before'))
        _cp.setdefault('after_path', _cp.get('after'))
    edge_previews["start"] = {"clip_path": edge_previews.get("source", {}).get("start_kept") or edge_previews.get("target", {}).get("start_kept")}
    edge_previews["end"]   = {"clip_path": edge_previews.get("source", {}).get("end_kept") or edge_previews.get("target", {}).get("end_kept")}
    src_start = src_trim_start
    src_end   = src_trim_end
    tgt_start = tgt_trim_start
    tgt_end   = tgt_trim_end
    progress_emit('scan', 0.95, 'Превью для проверки созданы', step='previews')
    report = {
        "version": "v4.2",
        "job_id": job_id_from_paths(source_path, target_path),
        "source_path": os.path.abspath(source_path),
        "target_path": os.path.abspath(target_path),
        "source_duration": source_duration,
        "target_duration": target_duration,
        "source_fps": source_fps,
        "target_fps": target_fps,
        "args": {
            "frame_diff_percentage": args.frame_diff_percentage,
            "small_threshold": args.small_threshold,
            "large_threshold": args.large_threshold,
            "trim_threshold": args.trim_threshold,
            "min_insert_cut": float(getattr(args, "min_insert_cut", 3.0) or 3.0),
            "min_insert_cut_shorter": (None if getattr(args, "min_insert_cut_shorter", None) is None else float(args.min_insert_cut_shorter)),
            "prefer_longer_cuts": bool(getattr(args, "prefer_longer_cuts", True)),
            "use_precise_scene_detect": bool(args.use_precise_scene_detect),
            "refine_method": args.refine_method,
            "cut_mode": args.cut_mode,
            "cut_pad": args.cut_pad,
            "deep_scan": not args.no_deep_scan_edges,
            "deep_region_mode": args.deep_region_mode,
            "deep_search_window": args.deep_search_window,
            "deep_miss_forward": args.deep_miss_forward,
            "deep_hash_threshold": args.deep_hash_threshold,
            "deep_end_snap": float(getattr(args, "deep_end_snap", 0.0) or 0.0),
            "keep_end_same_tail": float(getattr(args, "keep_end_same_tail", 0.0) or 0.0),
            "keep_end_same_tail_tol": float(getattr(args, "keep_end_same_tail_tol", 0.0) or 0.0),
            "keep_end_verify_window": float(getattr(args, "keep_end_verify_window", 0.0) or 0.0),
            "keep_end_verify_samples": int(getattr(args, "keep_end_verify_samples", 0) or 0),
            "keep_end_verify_hash_threshold": int(getattr(args, "keep_end_verify_hash_threshold", 0) or 0),
            "keep_end_dur_tol": float(getattr(args, "keep_end_dur_tol", 0.0) or 0.0),
            "keep_end_max_tail": float(getattr(args, "keep_end_max_tail", 0.0) or 0.0),
        },
        "deep_scan": {
            "ratio_start": float(r_start),
            "ratio_end": float(r_end),
        },
        "end_override": end_override,
        "trims_abs": {
            "source_start": float(src_trim_start),
            "source_end": float(src_trim_end),
            "target_start": float(tgt_trim_start),
            "target_end": float(tgt_trim_end),
        },
        "trims_len": {
            "source_start_trim": float(max(0.0, src_trim_start)),
            "source_end_trim": float(max(0.0, source_duration - src_trim_end)),
            "target_start_trim": float(max(0.0, tgt_trim_start)),
            "target_end_trim": float(max(0.0, target_duration - tgt_trim_end)),
            "source_kept_len": float(max(0.0, src_trim_end - src_trim_start)),
            "target_kept_len": float(max(0.0, tgt_trim_end - tgt_trim_start)),
        },
        "cuts_refined_abs": [
            {
                "video": s["video"],
                "cut_start_abs": float(s["cut_start_abs"]),
                "cut_end_abs": float(s["cut_end_abs"]),
                "insert_duration": float(s["insert_duration"]),
                "context": {
                    "source_start": float(s["source_start"]),
                    "source_end": float(s["source_end"]),
                    "target_start": float(s["target_start"]),
                    "target_end": float(s["target_end"]),
                    "difference": float(s["difference"]),
                }
            }
            for s in cut_segments_refined
        ],
        "ignored_cuts": [
            {
                "video": s.get("video"),
                "cut_start_abs": float(s.get("cut_start_abs", 0.0)),
                "cut_end_abs": float(s.get("cut_end_abs", 0.0)),
                "insert_duration": float(s.get("insert_duration", 0.0)),
                "difference": float(s.get("difference", s.get("insert_duration", 0.0) or 0.0) or 0.0),
                "difference_raw": float(s.get("difference_raw", s.get("difference", s.get("insert_duration", 0.0) or 0.0) or 0.0) or 0.0),
                "reason": s.get("ignored_reason", "filtered"),
                "context": {
                    "source_start": float(s.get("source_start", 0.0)),
                    "source_end": float(s.get("source_end", 0.0)),
                    "target_start": float(s.get("target_start", 0.0)),
                    "target_end": float(s.get("target_end", 0.0)),
                    "difference": float(s.get("difference", s.get("insert_duration", 0.0) or 0.0) or 0.0),
                },
            }
            for s in ignored_short_cuts
        ],
        "previews": {
            "edges": edges_for_report,
            "cuts": cut_previews
        }
    }
    # Generate interactive HTML report
    try:
        html_report_path = generate_html_report(report, workdir)
        report["html_report_path"] = html_report_path
        print(f"\n🌐 Интерактивный HTML-отчёт готов: {html_report_path}")
        if args.open_folders:
            open_folder(html_report_path)
    except Exception as e:
        print(f"⚠️ Не удалось создать HTML-отчёт: {e}")
    
    progress_emit('scan', 1.0, 'Сканирование завершено', step='done')
    return report


def save_report(report: dict, report_path: str):
    ensure_dir(os.path.dirname(report_path) or ".")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def load_report(report_path: str) -> Optional[dict]:
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------
# Finalize phase
# --------------------------

def perform_finalize(args, report: dict):
    ffmpeg_script = args.ffmpeg
    rubberband_script = args.rubberband
    imagemagick_script = args.imagemagick
    workdir = os.path.abspath(args.workdir)
    ensure_dir(workdir)
    source_path = args.source_path
    target_path = args.target_path
    source_audio_hz = get_audio_hz(source_path)
    trims = report["trims_abs"]
    src_trim_start = float(trims["source_start"])
    src_trim_end = float(trims["source_end"])
    tgt_trim_start = float(trims["target_start"])
    tgt_trim_end = float(trims["target_end"])
    inter_dir = ensure_dir(os.path.join(workdir, "intermediate"))
    source_trimmed = os.path.join(inter_dir, "source_trimmed" + os.path.splitext(source_path)[1])
    target_trimmed = os.path.join(inter_dir, "target_trimmed" + os.path.splitext(target_path)[1])
    progress_emit('finalize', 0.05, 'Обрезка краёв…', step='trim_edges')
    print("\n" + "=" * 80)
    print("✂️  Обрезка видео по границам (finalize)")
    print("=" * 80)
    source_trimmed, used_src_start, used_src_end = trim_video_edges_return(
        source_path, src_trim_start, src_trim_end, source_trimmed, args.trim_threshold, ffmpeg_script
    )
    target_trimmed, used_tgt_start, used_tgt_end = trim_video_edges_return(
        target_path, tgt_trim_start, tgt_trim_end, target_trimmed, args.trim_threshold, ffmpeg_script
    )
    if not getattr(args, 'no_normalize_ts', False):
        source_trimmed = normalize_media_timestamps(
            source_trimmed,
            os.path.join(inter_dir, 'source_trimmed_norm' + os.path.splitext(source_trimmed)[1]),
            ffmpeg_script,
        )
        target_trimmed = normalize_media_timestamps(
            target_trimmed,
            os.path.join(inter_dir, 'target_trimmed_norm' + os.path.splitext(target_trimmed)[1]),
            ffmpeg_script,
        )
    cuts = report.get("cuts_refined_abs", [])
    source_cuts_rel: List[dict] = []
    target_cuts_rel: List[dict] = []
    for s in cuts:
        v = s["video"]
        st_abs = float(s["cut_start_abs"])
        dur = float(s["insert_duration"])
        if v == "source":
            st_rel = st_abs - used_src_start
            if st_rel >= 0.0:
                source_cuts_rel.append({"cut_start_rel": st_rel, "insert_duration": dur})
        else:
            st_rel = st_abs - used_tgt_start
            if st_rel >= 0.0:
                target_cuts_rel.append({"cut_start_rel": st_rel, "insert_duration": dur})
    source_processed = source_trimmed
    target_processed = target_trimmed
    if source_cuts_rel:
        source_processed = os.path.join(inter_dir, "source_processed" + os.path.splitext(source_path)[1])
        print("\n🎬 Вырезаю вставки из SOURCE...")
        cut_large_inserts(
            source_trimmed, source_cuts_rel, source_processed,
            workdir=workdir,
            ffmpeg_script=ffmpeg_script,
            cut_mode=args.cut_mode,
            cut_pad=args.cut_pad,
            keep_audio=True,
            cut_crf=args.cut_crf,
            cut_preset=args.cut_preset,
        )
    if target_cuts_rel:
        target_processed = os.path.join(inter_dir, "target_processed" + os.path.splitext(target_path)[1])
        print("\n🎬 Вырезаю вставки из TARGET...")
        cut_large_inserts(
            target_trimmed, target_cuts_rel, target_processed,
            workdir=workdir,
            ffmpeg_script=ffmpeg_script,
            cut_mode=args.cut_mode,
            cut_pad=args.cut_pad,
            keep_audio=False,
            cut_crf=args.cut_crf,
            cut_preset=args.cut_preset,
        )
    if not getattr(args, 'no_normalize_ts', False):
        source_processed = normalize_media_timestamps(
            source_processed,
            os.path.join(inter_dir, 'source_processed_norm' + os.path.splitext(source_processed)[1]),
            ffmpeg_script,
        )
        target_processed = normalize_video_timestamps_only(
            target_processed,
            os.path.join(inter_dir, 'target_processed_vnorm' + os.path.splitext(target_processed)[1]),
            ffmpeg_script,
            force=True,
        )
    progress_emit('finalize', 0.25, 'Вставки вырезаны, начинаю перескан…', step='cut_done')
    print("\n" + "=" * 80)
    print("🎵 Синхронизация аудио (перескан обработанных видео + timemap)")
    print("=" * 80 + "\n")
    source_proc_duration = get_duration(source_processed)
    target_proc_duration = get_duration(target_processed)
    source_proc_fps = get_fps(source_processed)
    source_proc_tbn = get_tbn(source_processed)
    source_proc_pos_per_frame = source_proc_tbn / source_proc_fps
    source_proc_audio_samples_per_frame = source_audio_hz / source_proc_fps
    target_proc_fps = get_fps(target_processed)
    target_proc_tbn = get_tbn(target_processed)
    target_proc_pos_per_frame = target_proc_tbn / target_proc_fps
    frames_root = ensure_dir(os.path.join(workdir, "frames"))
    source_proc_frames = ensure_dir(os.path.join(frames_root, "SOURCE_PROCESSED_FRAMES"))
    target_proc_frames = ensure_dir(os.path.join(frames_root, "TARGET_PROCESSED_FRAMES"))
    progress_emit('finalize', 0.35, 'Детекция сцен (обработанный SOURCE)…', step='scene_source')
    print("   Сканирую source (обработанный)...")
    source_proc_list = capture_frame_info(
        video_path=source_processed,
        output_folder=source_proc_frames,
        cut_borders=args.source_cut_borders,
        frame_diff=args.frame_diff_percentage,
        video_tbn=source_proc_tbn,
        video_fps=source_proc_fps,
        video_pos_per_frame=source_proc_pos_per_frame,
        audio_samples_per_frame=source_proc_audio_samples_per_frame,
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        keep_frame_dumps=args.keep_frame_dumps,
    )
    progress_emit('finalize', 0.45, 'Детекция сцен (SOURCE) завершена', step='scene_source_done')
    print("   Сканирую target (обработанный)...")
    target_proc_list = capture_frame_info(
        video_path=target_processed,
        output_folder=target_proc_frames,
        cut_borders=args.target_cut_borders,
        frame_diff=args.frame_diff_percentage,
        video_tbn=target_proc_tbn,
        video_fps=target_proc_fps,
        video_pos_per_frame=target_proc_pos_per_frame,
        audio_samples_per_frame=0,
        ffmpeg_script=ffmpeg_script,
        imagemagick_script=imagemagick_script,
        keep_frame_dumps=args.keep_frame_dumps,
    )
    progress_emit('finalize', 0.55, 'Детекция сцен (TARGET) завершена', step='scene_target_done')
    source_proc_info = {f["scene_frame_index"]: f for f in source_proc_list}
    target_proc_info = {f["scene_frame_index"]: f for f in target_proc_list}
    print("\n🔍 Поиск совпадений в обработанных видео...")
    if len(target_proc_list) < len(source_proc_list):
        processed_twins2_all = find_twin_frames(target_proc_list, source_proc_list, reverse_main_and_twin=True)
    else:
        processed_twins2_all = find_twin_frames(source_proc_list, target_proc_list, reverse_main_and_twin=False)
    print(f"✅ Найдено {len(processed_twins2_all)} совпадений\n")
    progress_emit('finalize', 0.60, f'Совпадения найдены: {len(processed_twins2_all)}', step='matches')
    if len(processed_twins2_all) < 3:
        raise RuntimeError("Too few matches after processing, cannot build timemap")
    if args.auto_sync:
        s2, e2 = auto_find_sync_points(processed_twins2_all, source_proc_info, target_proc_info)
        processed_twins2 = processed_twins2_all[s2:e2 + 1]
        if len(processed_twins2) < 3:
            processed_twins2 = processed_twins2_all
    else:
        processed_twins2 = processed_twins2_all
    fmt = choose_audio_work_format(source_audio_hz)
    audio_dir = ensure_dir(os.path.join(workdir, "audio"))
    temp_audio_in = os.path.join(audio_dir, f"rb_input_audio.{fmt}")
    synced_audio = os.path.join(audio_dir, f"synced_audio.{fmt}")
    print("🔊 Извлекаю и готовлю аудио из source_processed...")
    ok = extract_audio_for_rubberband(source_processed, temp_audio_in, source_audio_hz, ffmpeg_script)
    if not ok:
        raise RuntimeError("Cannot extract audio for rubberband")
    timemap_dir = ensure_dir(os.path.join(workdir, "timemap"))
    timecodes_file = os.path.join(timemap_dir, "timecodes_processed.txt")
    print(f"\n📝 Создаю timemap на основе {len(processed_twins2)} совпадений...")
    create_rubberband_timemap_simple(processed_twins2, source_proc_info, target_proc_info, source_audio_hz, timecodes_file)
    print(f"✅ Timemap: {timecodes_file}")
    progress_emit('finalize', 0.68, 'Timemap готов, запускаю rubberband…', step='timemap')
    timing_ratio = target_proc_duration / max(0.001, source_proc_duration)
    print("\n🎵 Применяю rubberband с timemap...")
    print(f"   Base ratio: {timing_ratio:.6f}")
    print(f"   Source processed: {source_proc_duration:.2f}s")
    print(f"   Target processed: {target_proc_duration:.2f}s")
    rb_cmd = (
        f'"{rubberband_script}" --timemap "{timecodes_file}" '
        f'-t {timing_ratio:.8f} "{temp_audio_in}" "{synced_audio}"'
    )
    res = subprocess.run(rb_cmd, shell=True, capture_output=True, text=True)
    if not (os.path.exists(synced_audio) and os.path.getsize(synced_audio) > 1000):
        print("⚠️ Rubberband failed; using ffmpeg atempo fallback (linear)")
        ok2 = apply_audio_stretch_ffmpeg(temp_audio_in, synced_audio, timing_ratio, ffmpeg_script)
        if not ok2:
            if res.stderr:
                print(res.stderr[:1200])
            raise RuntimeError("Audio stretch failed")
    progress_emit('finalize', 0.82, 'Аудио синхронизировано', step='audio_done')
    if args.output:
        final_output = os.path.abspath(args.output)
    else:
        root, ext = os.path.splitext(os.path.abspath(target_path))
        final_output = f"{root}_FINAL{ext}"
    progress_emit('finalize', 0.90, 'Сборка финального файла…', step='mux')
    print("\n🎬 Объединяю видео target_processed + синхронизированное аудио...")
    ensure_dir(os.path.dirname(final_output) or ".")
    target_video_for_mux = target_processed
    if not getattr(args, "no_normalize_ts", False):
        target_video_for_mux = normalize_video_timestamps_only(
            target_processed,
            os.path.join(inter_dir, "target_video_for_mux" + os.path.splitext(target_processed)[1]),
            ffmpeg_script,
            force=True,
        )
    out_ext = os.path.splitext(final_output)[1].lower()
    audio_codec = getattr(args, "final_audio_codec", "auto")
    if audio_codec == "auto":
        if out_ext in (".mp4", ".m4v", ".mov"):
            audio_codec = "aac"
        else:
            in_aext = os.path.splitext(synced_audio)[1].lower()
            if in_aext in (".opus", ".flac"):
                audio_codec = "copy"
            else:
                audio_codec = "opus"
    if out_ext in (".mp4", ".m4v", ".mov") and audio_codec in ("opus", "flac", "copy"):
        audio_codec = "aac"
    video_mode = getattr(args, "final_video_mode", "copy")
    cmd_parts = [
        ffmpeg_script,
        "-y",
        "-i",
        f'"{target_video_for_mux}"',
        "-i",
        f'"{synced_audio}"',
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ]
    if video_mode == "copy":
        cmd_parts += ["-c:v", "copy"]
    else:
        cmd_parts += [
            "-c:v", "libx264",
            "-preset", str(getattr(args, "final_preset", "veryfast")),
            "-crf", str(int(getattr(args, "final_crf", 18))),
            "-pix_fmt", "yuv420p",
        ]
    bitrate = str(getattr(args, "final_audio_bitrate", "192k"))
    use_shortest = True
    if audio_codec == "copy":
        cmd_parts += ["-c:a", "copy"]
        try:
            adur = get_duration(synced_audio)
            vdur = get_duration(target_video_for_mux)
            if adur + 0.05 < vdur:
                use_shortest = False
        except Exception:
            use_shortest = False
    elif audio_codec == "flac":
        cmd_parts += ["-c:a", "flac", "-af", "apad"]
    elif audio_codec == "opus":
        cmd_parts += ["-c:a", "libopus", "-b:a", bitrate, "-af", "apad"]
    else:
        cmd_parts += ["-c:a", "aac", "-b:a", bitrate, "-af", "apad"]
    if use_shortest:
        cmd_parts += ["-shortest"]
    cmd_parts += [f'"{final_output}"']
    merge_cmd = " ".join(cmd_parts)
    merge_res = ffmpeg_run(merge_cmd, quiet=False, capture_output=True, purpose="mux_final")
    if not os.path.exists(final_output):
        if merge_res.stderr:
            print(merge_res.stderr[:1600])
        raise RuntimeError("Final merge failed")
    progress_emit('finalize', 0.97, 'Финальный файл создан', step='mux_done')
    final_duration = get_duration(final_output)
    print("\n" + "=" * 80)
    print("✅ ГОТОВО!")
    print("=" * 80)
    print(f"📁 Финальный файл: {final_output}")
    print(f"⏱️  Длительность: {final_duration:.1f}s")
    print(f"📊 Вырезано вставок: {len(source_cuts_rel) + len(target_cuts_rel)}")
    print(f"🎯 Cut mode: {args.cut_mode}, pad={args.cut_pad}s")
    print("=" * 80)
    if args.open_folders:
        open_folder(os.path.dirname(final_output))
    try:
        report["final_output"] = final_output
        report["output_path"] = final_output
        report["final_duration"] = float(final_duration)
        if getattr(args, "report", None):
            save_report(report, args.report)
    except Exception:
        pass
    if getattr(args, "cleanup_temp", False):
        cleanup_workdir_temp(workdir)
    progress_emit('finalize', 1.0, 'Финализация завершена', step='done')


# --------------------------
# HTML Report Generator (Timeline Preview)
# --------------------------
def generate_html_report(report: dict, workdir: str) -> str:
    html_path = os.path.join(workdir, "preview_report.html")
    src_dur = report["source_duration"]
    trims = report["trims_abs"]
    cuts = report.get("cuts_refined_abs", [])
    previews = report.get("previews", {})
    edges = previews.get("edges", {})
    cuts_previews = previews.get("cuts", [])
    timeline = []
    if trims["source_start"] > 0.1:
        timeline.append({"type": "trim", "start": 0, "end": trims["source_start"],
                        "color": "#475569", "label": f"Обрезано: {trims['source_start']:.1f}s"})
    last_time = trims["source_start"]
    for cut in sorted([c for c in cuts if c["video"] == "source"], key=lambda x: x["cut_start_abs"]):
        if cut["cut_start_abs"] > last_time + 0.1:
            timeline.append({"type": "keep", "start": last_time, "end": cut["cut_start_abs"],
                            "color": "#16a34a", "label": "Контент"})
        timeline.append({"type": "cut", "start": cut["cut_start_abs"], "end": cut["cut_end_abs"],
                        "color": "#dc2626", "label": f"Вырез: {cut['insert_duration']:.1f}s"})
        last_time = cut["cut_end_abs"]
    if trims["source_end"] > last_time + 0.1:
        timeline.append({"type": "keep", "start": last_time, "end": trims["source_end"],
                        "color": "#16a34a", "label": "Контент"})
    if src_dur - trims["source_end"] > 0.1:
        timeline.append({"type": "trim", "start": trims["source_end"], "end": src_dur,
                        "color": "#475569", "label": f"Обрезано: {src_dur - trims['source_end']:.1f}s"})
    total_cut_time = sum(c["insert_duration"] for c in cuts if c["video"] == "source")
    def rel_path(p):
        if not p: return ""
        try:
            return os.path.relpath(p, workdir).replace("\\", "/")
        except Exception:
            return ""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Отчёт по вырезанию — {os.path.basename(report.get('source_path',''))}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ background: #0f172a; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif;
         margin: 0; padding: 25px 40px; line-height: 1.5; }}
  h1 {{ color: #f1f5f9; border-bottom: 2px solid #334155; padding-bottom: 15px; margin-top: 0;
        display: flex; align-items: center; gap: 15px; }}
  h2 {{ color: #cbd5e1; margin-top: 50px; display: flex; align-items: center; gap: 12px; }}
  .stats {{ display: flex; gap: 15px; margin: 25px 0; flex-wrap: wrap; }}
  .stat {{ background: #1e293b; padding: 15px 22px; border-radius: 10px;
           border: 1px solid #334155; flex: 1; min-width: 180px; }}
  .stat .value {{ font-size: 26px; font-weight: bold; color: #60a5fa; font-family: monospace; }}
  .stat .label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
  .timeline-wrap {{ background: #1e293b; padding: 20px; border-radius: 12px;
                    border: 1px solid #334155; margin: 20px 0; }}
  .timeline {{ height: 50px; border-radius: 6px; position: relative; overflow: hidden;
               background: #0f172a; border: 1px solid #0f172a; }}
  .segment {{ position: absolute; top: 0; height: 100%; display: flex; align-items: center;
              justify-content: center; font-size: 11px; font-weight: 600; cursor: pointer;
              transition: filter 0.2s; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; padding: 0 6px; color: #fff; }}
  .segment:hover {{ filter: brightness(1.4); z-index: 10; box-shadow: 0 0 0 2px #fff; }}
  .legend {{ display: flex; gap: 20px; margin-top: 15px; font-size: 13px; color: #cbd5e1; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 14px; height: 14px; border-radius: 3px; }}
  .section {{ margin: 30px 0; background: #1e293b; padding: 25px;
              border-radius: 12px; border: 1px solid #334155; }}
  .badge {{ padding: 5px 12px; border-radius: 6px; font-size: 12px; font-weight: bold;
            letter-spacing: 0.5px; }}
  .badge-keep {{ background: #166534; color: #bbf7d0; }}
  .badge-cut {{ background: #991b1b; color: #fecaca; }}
  .badge-trim {{ background: #475569; color: #e2e8f0; }}
  .video-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
                 gap: 18px; margin-top: 15px; }}
  .video-card {{ background: #0f172a; padding: 15px; border-radius: 10px;
                 border: 1px solid #334155; }}
  .video-card h3 {{ margin: 0 0 12px 0; font-size: 13px; text-transform: uppercase;
                    letter-spacing: 1px; display: flex; align-items: center; gap: 8px; }}
  .video-card h3.cut {{ color: #f87171; }}
  .video-card h3.keep {{ color: #4ade80; }}
  .video-card h3.trim {{ color: #94a3b8; }}
  video {{ width: 100%; border-radius: 6px; background: #000; display: block; }}
  .frames {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
             gap: 8px; margin-top: 15px; }}
  .frames img {{ width: 100%; border-radius: 4px; border: 1px solid #334155;
                 cursor: pointer; transition: transform 0.2s; }}
  .frames img:hover {{ transform: scale(1.05); border-color: #60a5fa; }}
  .cut-card {{ background: #0f172a; border-left: 4px solid #ef4444; padding: 22px;
               border-radius: 8px; margin-bottom: 25px; }}
  .cut-card h3 {{ margin-top: 0; color: #f1f5f9; font-size: 18px; }}
  .info-row {{ display: flex; justify-content: space-between; padding: 6px 0;
               border-bottom: 1px dashed #334155; font-size: 13px; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-label {{ color: #94a3b8; }}
  .info-value {{ font-family: 'Consolas', monospace; color: #f1f5f9; }}
  .no-cut {{ text-align: center; color: #64748b; padding: 30px; font-style: italic; }}
  details {{ background: #0f172a; padding: 12px 18px; border-radius: 6px; margin-top: 15px;
             border: 1px solid #334155; }}
  summary {{ cursor: pointer; color: #60a5fa; font-weight: 500; }}
  details[open] summary {{ margin-bottom: 10px; }}
</style>
</head>
<body>
<h1>🎬 Отчёт по вырезанию рекламы
    <span style="font-size:14px; color:#94a3b8; font-weight:normal;">{os.path.basename(report.get('source_path',''))}</span>
</h1>
<div class="stats">
  <div class="stat"><div class="value">{src_dur:.1f}s</div><div class="label">Длительность Source</div></div>
  <div class="stat"><div class="value">{len([c for c in cuts if c['video']=='source'])}</div><div class="label">Вырезано вставок</div></div>
  <div class="stat"><div class="value">{total_cut_time:.1f}s</div><div class="label">Общая длина вырезанного</div></div>
  <div class="stat"><div class="value">{trims['source_start']:.1f}s / {(src_dur - trims['source_end']):.1f}s</div><div class="label">Обрезано начало / конец</div></div>
</div>
<div class="timeline-wrap">
  <h2 style="margin-top:0;">📊 Таймлайн Source</h2>
  <div class="timeline">
"""
    for i, seg in enumerate(timeline):
        left = (seg["start"] / src_dur) * 100
        width = max(((seg["end"] - seg["start"]) / src_dur) * 100, 0.3)
        target_id = "trim-start" if seg["type"] == "trim" and i == 0 else \
                    "trim-end" if seg["type"] == "trim" else f"cut-{i}"
        html += f'<div class="segment" style="left:{left:.2f}%; width:{width:.2f}%; background:{seg["color"]};" '
        html += f'title="{seg["label"]} ({seg["start"]:.1f}s - {seg["end"]:.1f}s)" '
        html += f'onclick="document.getElementById(\'{target_id}\').scrollIntoView({{behavior:\'smooth\'}})">'
        html += f'{seg["label"] if width > 6 else ""}</div>\n'
    html += """  </div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#16a34a"></div>Оставленный контент</div>
    <div class="legend-item"><div class="legend-dot" style="background:#dc2626"></div>Вырезанная реклама</div>
    <div class="legend-item"><div class="legend-dot" style="background:#475569"></div>Обрезанные края</div>
  </div>
</div>
<h2>✂️ Начало видео <span class="badge badge-trim">START</span></h2>
<div class="section" id="trim-start">
  <div class="video-grid">
"""
    if edges.get("start_removed"):
        p = rel_path(edges["start_removed"]["clip_path"])
        html += f'''<div class="video-card">
      <h3 class="trim">🗑️ ВЫРЕЗАНО ({trims["source_start"]:.2f}s)</h3>
      <video controls preload="metadata"><source src="{p}" type="video/mp4"></video>
    </div>\n'''
    if edges.get("start"):
        p = rel_path(edges["start"]["clip_path"])
        html += f'''<div class="video-card">
      <h3 class="keep">✅ ОСТАВЛЕНО (первые ~3s)</h3>
      <video controls preload="metadata"><source src="{p}" type="video/mp4"></video>
    </div>\n'''
    html += '</div></div>\n'
    src_cuts = [cp for cp in cuts_previews if cp.get("video") == "source"]
    if src_cuts:
        html += '<h2>🎯 Вырезанные вставки <span class="badge badge-cut">CUT</span></h2>\n'
        html += '<div class="section" id="cuts">\n'
        for i, cp in enumerate(src_cuts, 1):
            dur = cp.get("duration", 0)
            html += f'<div class="cut-card" id="cut-{i}">\n'
            html += f'<h3>Вставка #{i} — {dur:.2f}s '
            html += f'<span style="font-size:13px; color:#94a3b8; font-weight:normal;">'
            html += f'({cp["cut_start_abs"]:.2f}s → {cp["cut_end_abs"]:.2f}s)</span></h3>\n'
            html += f'<div class="info-row"><span class="info-label">Длительность:</span><span class="info-value">{dur:.3f}s</span></div>\n'
            html += f'<div class="info-row"><span class="info-label">Таймкод начала:</span><span class="info-value">{cp["cut_start_abs"]:.3f}s</span></div>\n'
            html += f'<div class="info-row"><span class="info-label">Таймкод конца:</span><span class="info-value">{cp["cut_end_abs"]:.3f}s</span></div>\n'
            html += '<div class="video-grid" style="margin-top:15px;">\n'
            if cp.get("before"):
                html += f'''<div class="video-card"><h3 class="keep">➡️ ДО вырезки</h3>
                <video controls preload="metadata"><source src="{rel_path(cp['before'])}" type="video/mp4"></video></div>\n'''
            if cp.get("cut"):
                html += f'''<div class="video-card"><h3 class="cut">🔴 САМА ВСТАВКА</h3>
                <video controls preload="metadata"><source src="{rel_path(cp['cut'])}" type="video/mp4"></video></div>\n'''
            if cp.get("after"):
                html += f'''<div class="video-card"><h3 class="keep">⬅️ ПОСЛЕ вырезки</h3>
                <video controls preload="metadata"><source src="{rel_path(cp['after'])}" type="video/mp4"></video></div>\n'''
            html += '</div>\n'
            frames_dir = cp.get("frames_dir", "")
            if frames_dir and os.path.isdir(frames_dir):
                frames = sorted([f for f in os.listdir(frames_dir) if f.lower().endswith(".jpg")])
                if frames:
                    html += '<details><summary>📸 Показать кадры из вставки (превью)</summary>\n'
                    html += '<div class="frames">\n'
                    for f in frames:
                        fpath = os.path.join(frames_dir, f).replace("\\", "/")
                        html += f'<img src="{rel_path(fpath)}" loading="lazy">\n'
                    html += '</div></details>\n'
            html += '</div>\n'
        html += '</div>\n'
    else:
        html += '<div class="section"><div class="no-cut">✨ Вставок не обнаружено — видео чистое!</div></div>\n'
    html += '<h2>🏁 Конец видео <span class="badge badge-trim">END</span></h2>\n'
    html += '<div class="section" id="trim-end">\n  <div class="video-grid">\n'
    if edges.get("end"):
        p = rel_path(edges["end"]["clip_path"])
        html += f'''<div class="video-card">
      <h3 class="keep">✅ ОСТАВЛЕНО (последние ~3s)</h3>
      <video controls preload="metadata"><source src="{p}" type="video/mp4"></video>
    </div>\n'''
    if edges.get("end_removed"):
        p = rel_path(edges["end_removed"]["clip_path"])
        html += f'''<div class="video-card">
      <h3 class="trim">🗑️ ВЫРЕЗАНО ({src_dur - trims["source_end"]:.2f}s)</h3>
      <video controls preload="metadata"><source src="{p}" type="video/mp4"></video>
    </div>\n'''
    html += '</div></div>\n'
    html += """
<script>
  document.querySelectorAll('video').forEach(v => {
    v.addEventListener('play', function() {
      document.querySelectorAll('video').forEach(other => {
        if (other !== v) other.pause();
      });
    });
  });
  document.querySelectorAll('.frames img').forEach(img => {
    img.addEventListener('click', () => window.open(img.src, '_blank'));
  });
</script>
</body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path


# --------------------------
# GUI
# --------------------------
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    import threading
    import subprocess
    import json
    import os
    import sys

    class App:
        def __init__(self, root):
            self.root = root
            self.root.title("ADS Video Sync - Portable GUI")
            self.root.geometry("950x750")
            if getattr(sys, 'frozen', False):
                self.runner = [sys.executable]
            else:
                self.runner = [sys.executable, os.path.abspath(__file__)]
            self.process = None
            self.setup_ui()
            
        def setup_ui(self):
            style = ttk.Style()
            try: style.theme_use('clam')
            except: pass
            frm_files = ttk.LabelFrame(self.root, text="Файлы", padding=10)
            frm_files.pack(fill='x', padx=10, pady=5)
            self.source_var = tk.StringVar()
            self.target_var = tk.StringVar()
            self.workdir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "ads_workdir"))
            ttk.Label(frm_files, text="Source (Неправильный тайминг):").grid(row=0, column=0, sticky='w', pady=2)
            ttk.Entry(frm_files, textvariable=self.source_var, width=80).grid(row=0, column=1, padx=5, pady=2)
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse(self.source_var)).grid(row=0, column=2, pady=2)
            ttk.Label(frm_files, text="Target (Правильный тайминг):").grid(row=1, column=0, sticky='w', pady=2)
            ttk.Entry(frm_files, textvariable=self.target_var, width=80).grid(row=1, column=1, padx=5, pady=2)
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse(self.target_var)).grid(row=1, column=2, pady=2)
            ttk.Label(frm_files, text="Рабочая папка:").grid(row=2, column=0, sticky='w', pady=2)
            ttk.Entry(frm_files, textvariable=self.workdir_var, width=80).grid(row=2, column=1, padx=5, pady=2)
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse_dir(self.workdir_var)).grid(row=2, column=2, pady=2)
            frm_opts = ttk.LabelFrame(self.root, text="Опции", padding=10)
            frm_opts.pack(fill='x', padx=10, pady=5)
            self.auto_sync = tk.BooleanVar(value=True)
            self.deep_scan = tk.BooleanVar(value=True)
            self.precise = tk.BooleanVar(value=True)
            ttk.Checkbutton(frm_opts, text="Auto Sync", variable=self.auto_sync).grid(row=0, column=0, padx=5, sticky='w')
            ttk.Checkbutton(frm_opts, text="Deep Scan Edges", variable=self.deep_scan).grid(row=0, column=1, padx=5, sticky='w')
            ttk.Checkbutton(frm_opts, text="Precise Scene Detect (Refine)", variable=self.precise).grid(row=0, column=2, padx=5, sticky='w')
            frm_ctrl = ttk.Frame(self.root, padding=10)
            frm_ctrl.pack(fill='x', padx=10)
            self.btn_scan = ttk.Button(frm_ctrl, text="1. Сканировать и создать превью", command=self.run_scan)
            self.btn_scan.pack(side='left', padx=5)
            self.btn_finalize = ttk.Button(frm_ctrl, text="2. Финализировать (Собрать видео)", command=self.run_finalize, state='disabled')
            self.btn_finalize.pack(side='left', padx=5)
            self.btn_open = ttk.Button(frm_ctrl, text="Открыть рабочую папку", command=self.open_workdir)
            self.btn_open.pack(side='left', padx=5)
            self.btn_report = ttk.Button(frm_ctrl, text="🎬 Открыть HTML отчёт", command=self.open_html_report)
            self.btn_report.pack(side='left', padx=5)
            self.btn_stop = ttk.Button(frm_ctrl, text="Остановить", command=self.stop_process, state='disabled')
            self.btn_stop.pack(side='right', padx=5)
            self.progress = ttk.Progressbar(self.root, mode='determinate')
            self.progress.pack(fill='x', padx=10, pady=5)
            self.status_var = tk.StringVar(value="Готов к работе.")
            ttk.Label(self.root, textvariable=self.status_var).pack(fill='x', padx=10)
            self.log_text = scrolledtext.ScrolledText(self.root, height=15, wrap='word', font=("Consolas", 10))
            self.log_text.pack(fill='both', expand=True, padx=10, pady=5)
            
        def browse(self, var):
            path = filedialog.askopenfilename(filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov")])
            if path: var.set(path)
            
        def browse_dir(self, var):
            path = filedialog.askdirectory()
            if path: var.set(path)
            
        def open_workdir(self):
            if os.path.exists(self.workdir_var.get()):
                os.startfile(self.workdir_var.get())
        
        def open_html_report(self):
            report_path = os.path.join(self.workdir_var.get(), "preview_report.html")
            if os.path.exists(report_path):
                os.startfile(report_path)
            else:
                messagebox.showwarning("Нет отчёта", 
                    "HTML-отчёт ещё не создан.\nСначала нажмите 'Сканировать'.")
                
        def log(self, msg):
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            
        def build_cmd(self, phase):
            cmd = self.runner + [
                "--phase", phase, "--workdir", self.workdir_var.get(),
                "-sp", self.source_var.get(), "-tp", self.target_var.get(),
                "--progress", "-st", "2.0", "-lt", "5.0",
                "--deep-search-window", "1.3", "--deep-miss-forward", "35",
                "--deep-hash-threshold", "18", "--deep-region-mode", "multi"
            ]
            if self.auto_sync.get(): cmd.append("--auto-sync")
            if not self.deep_scan.get(): cmd.append("--no-deep-scan-edges")
            if self.precise.get(): cmd.append("--use-precise-scene-detect")
            return cmd
            
        def run_process(self, phase):
            if not self.source_var.get() or not self.target_var.get():
                messagebox.showerror("Ошибка", "Выберите Source и Target файлы!")
                return
            self.btn_scan.config(state='disabled')
            self.btn_finalize.config(state='disabled')
            self.btn_stop.config(state='normal')
            self.progress['value'] = 0
            self.log(f"--- Запуск фазы: {phase} ---")
            cmd = self.build_cmd(phase)
            def target():
                try:
                    kwargs = dict(
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8', errors='replace', bufsize=1
                    )
                    if os.name == 'nt':
                        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                    self.process = subprocess.Popen(cmd, **kwargs)
                    for line in self.process.stdout:
                        line = line.strip()
                        if line.startswith("@@PROGRESS@@"):
                            try:
                                data = json.loads(line.replace("@@PROGRESS@@", ""))
                                p = float(data.get("p", 0)) * 100
                                msg = data.get("msg", "")
                                self.root.after(0, self.update_progress, p, msg)
                            except: pass
                        else:
                            if line: self.root.after(0, self.log, line)
                    self.process.wait()
                    rc = self.process.returncode
                    self.root.after(0, self.on_finish, phase, rc)
                except Exception as e:
                    self.root.after(0, self.log, f"CRITICAL ERROR: {e}")
                    self.root.after(0, self.on_finish, phase, -1)
            threading.Thread(target=target, daemon=True).start()
            
        def update_progress(self, val, msg):
            self.progress['value'] = val
            self.status_var.set(msg)
            
        def on_finish(self, phase, rc):
            self.btn_stop.config(state='disabled')
            self.process = None
            if rc == 0:
                self.log(f"✅ Фаза {phase} успешно завершена!")
                self.status_var.set("Готово!")
                if phase == "scan":
                    self.btn_finalize.config(state='normal')
                    messagebox.showinfo("Успех", 
                        "Сканирование завершено!\n\n"
                        "🎬 Нажмите '🎬 Открыть HTML отчёт' чтобы просмотреть все вырезанные моменты.\n\n"
                        "Затем нажмите '2. Финализировать'.")
                else:
                    messagebox.showinfo("Успех", "Финализация завершена! Видео готово.")
            else:
                self.log(f"❌ Ошибка выполнения (код {rc}).")
                self.status_var.set("Ошибка!")
                self.btn_scan.config(state='normal')
                if phase == "finalize": self.btn_finalize.config(state='normal')
                    
        def run_scan(self): self.run_process("scan")
        def run_finalize(self): self.run_process("finalize")
            
        def stop_process(self):
            if self.process:
                self.process.terminate()
                self.log("Остановка процесса...")

    root = tk.Tk()
    App(root)
    root.mainloop()


# --------------------------
# Main
# --------------------------

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Video sync (workdir-safe scan/finalize pipeline)")
    parser.add_argument("--phase", choices=["all", "scan", "finalize"], default="all")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--report", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--no-normalize-ts", action="store_true")
    parser.add_argument("-sp", "--source-path", required=True)
    parser.add_argument("-tp", "--target-path", required=True)
    parser.add_argument("-scb", "--source-cut-borders", action="store_true")
    parser.add_argument("-tcb", "--target-cut-borders", action="store_true")
    parser.add_argument("-fdp", "--frame-diff-percentage", type=float, default=20.0)
    parser.add_argument("-st", "--small-threshold", type=float, default=2.0)
    parser.add_argument("-lt", "--large-threshold", type=float, default=5.0)
    parser.add_argument("-tt", "--trim-threshold", type=float, default=1.0)
    parser.add_argument("-sw", "--search-window", type=float, default=5.0)
    parser.add_argument("-auto", "--auto-sync", action="store_true", default=True)
    parser.add_argument("-psd", "--use-precise-scene-detect", action="store_true")
    parser.add_argument("--refine-method", choices=["alignment", "scenedetect"], default="alignment")
    parser.add_argument("--refine-hash-threshold", type=int, default=10)
    parser.add_argument("--refine-coarse-step", type=float, default=0.20)
    parser.add_argument("--refine-iters", type=int, default=18)
    parser.add_argument("--no-deep-scan-edges", action="store_true")
    parser.add_argument("--deep-scan-step", type=float, default=0.50)
    parser.add_argument("--deep-scan-back", type=float, default=600.0)
    parser.add_argument("--deep-hash-threshold", type=int, default=16)
    parser.add_argument("--match-hash-threshold", type=int, default=10)
    parser.add_argument("--match-min-count", type=int, default=80)
    parser.add_argument("--deep-miss-forward", type=int, default=20)
    parser.add_argument("--deep-miss-backward", type=int, default=12)
    parser.add_argument("--deep-search-window", type=float, default=1.0)
    parser.add_argument("--deep-search-step", type=float, default=0.08)
    parser.add_argument("--deep-verify-offset", type=float, default=0.18)
    parser.add_argument("--deep-region-mode", choices=["full", "center", "multi"], default="multi")
    parser.add_argument("--deep-quality-threshold", type=float, default=1.0)
    parser.add_argument("--deep-end-snap", type=float, default=6.0)
    parser.add_argument("--min-cut-len", type=float, default=2.0)
    parser.add_argument("--micro-verify", dest="micro_verify", action="store_true", default=True)
    parser.add_argument("--no-micro-verify", dest="micro_verify", action="store_false")
    parser.add_argument("--micro-verify-max-len", type=float, default=6.0)
    parser.add_argument("--micro-verify-step", type=float, default=0.10)
    parser.add_argument("--micro-verify-jitters", type=str, default="0,0.05")
    parser.add_argument("--micro-verify-window", type=float, default=0.60)
    parser.add_argument("--micro-verify-hash-threshold", type=int, default=0)
    parser.add_argument("--micro-verify-keep-max-match", type=float, default=0.20)
    parser.add_argument("--micro-verify-min-gap", type=float, default=0.60)
    parser.add_argument("--min-insert-cut", type=float, default=3.0)
    parser.add_argument("--min-insert-cut-shorter", type=float, default=None)
    parser.add_argument("--prefer-longer-cuts", dest="prefer_longer_cuts", action="store_true", default=True)
    parser.add_argument("--no-prefer-longer-cuts", dest="prefer_longer_cuts", action="store_false")
    parser.add_argument("--cut-mode", choices=["copy", "reencode"], default="copy")
    parser.add_argument("--cut-pad", type=float, default=0.15)
    parser.add_argument("--cut-crf", type=int, default=18)
    parser.add_argument("--cut-preset", default="veryfast")
    parser.add_argument("--final-video-mode", choices=["copy", "reencode"], default="copy")
    parser.add_argument("--final-crf", type=int, default=18)
    parser.add_argument("--final-preset", default="veryfast")
    parser.add_argument("--final-audio-codec", choices=["auto", "aac", "opus", "flac", "copy"], default="auto")
    parser.add_argument("--final-audio-bitrate", type=str, default="192k")
    parser.add_argument("--cleanup-temp", action="store_true")
    parser.add_argument("--open-folders", action="store_true")
    parser.add_argument("--keep-frame-dumps", action="store_true")
    parser.add_argument("-ff", "--ffmpeg", default=FFMPEG_BIN)
    parser.add_argument("-rb", "--rubberband", default=RUBBERBAND_BIN)
    parser.add_argument("-im", "--imagemagick", default=MAGICK_BIN)
    parser.add_argument('--ffmpeg-max-procs', type=int, default=2)
    parser.add_argument('--ffmpeg-lock-dir', default=None)
    parser.add_argument('--keep-end-dur-tol', dest='keep_end_dur_tol', type=float, default=2.0)
    parser.add_argument('--keep-end-max-tail', dest='keep_end_max_tail', type=float, default=15.0)
    parser.add_argument('--keep-end-same-tail', dest='keep_end_same_tail', type=float, default=15.0)
    parser.add_argument('--keep-end-same-tail-tol', dest='keep_end_same_tail_tol', type=float, default=0.50)
    parser.add_argument('--keep-end-verify-window', dest='keep_end_verify_window', type=float, default=15.0)
    parser.add_argument('--keep-end-verify-samples', dest='keep_end_verify_samples', type=int, default=4)
    parser.add_argument('--keep-end-verify-hash-threshold', dest='keep_end_verify_hash_threshold', type=int, default=0)
    args = parser.parse_args()
    set_progress_enabled(bool(getattr(args, 'progress', False)))
    args.workdir = os.path.abspath(args.workdir)
    ensure_dir(args.workdir)
    lock_dir = args.ffmpeg_lock_dir
    if not lock_dir:
        try:
            runs_root = os.path.dirname(args.workdir.rstrip('\\/'))
            if runs_root:
                lock_dir = os.path.join(runs_root, '_ffmpeg_semaphore')
        except Exception:
            lock_dir = None
    init_ffmpeg_limiter(args.ffmpeg_max_procs, lock_dir)
    report_path = args.report or os.path.join(args.workdir, "scan_report.json")
    if not os.path.isfile(args.source_path):
        print(f"Source video file does not exist: {args.source_path}")
        sys.exit(2)
    if not os.path.isfile(args.target_path):
        print(f"Target video file does not exist: {args.target_path}")
        sys.exit(2)
    try:
        if args.phase in ("scan", "all"):
            report = perform_scan(args)
            save_report(report, report_path)
            print(f"\n📝 Scan report saved: {report_path}")
            if args.phase == "scan":
                return
        if args.phase in ("finalize", "all"):
            report = load_report(report_path)
            if report is None:
                print("⚠️ scan_report.json not found or invalid, running scan now...")
                report = perform_scan(args)
                save_report(report, report_path)
            perform_finalize(args, report)
    except Exception as e:
        print("\n❌ ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 1 or "--gui" in sys.argv:
        try:
            launch_gui()
        except Exception as e:
            print(f"GUI Error: {e}")
            main()
    else:
        main()
