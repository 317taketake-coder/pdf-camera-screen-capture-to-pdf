"""
PDF Camera + Capture
画面上の任意の範囲を連続キャプチャして PDF にまとめる Windows 専用ツール。

必要なパッケージ:
    pip install pillow img2pdf
(keyboard は不要になりました。Windows 標準の RegisterHotKey を使います)
"""

import ctypes
import os
import queue
import re
import threading
import tempfile
import shutil
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

import img2pdf
import winsound
from PIL import Image, ImageDraw, ImageGrab, ImageOps, ImageTk


# ============================================================
# DPI 対策
# 高 DPI 環境で画面座標とキャプチャ範囲がずれるのを防ぐ。
# Per-Monitor v2 → Per-Monitor → System の順に、通ったものを使う。
# ============================================================
def enable_dpi_awareness():
    try:
        # PER_MONITOR_AWARE_V2 (Windows 10 1703 以降)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()


# ============================================================
# 仮想デスクトップ全体の範囲を取得
# winfo_screenwidth() はプライマリモニタしか返さないため、
# マルチモニタではオーバーレイがサブモニタに広がらない。
# ============================================================
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def get_virtual_screen():
    """(x, y, width, height) を返す。左や上にモニタがあると x, y は負になる。"""
    gsm = ctypes.windll.user32.GetSystemMetrics
    return (
        gsm(SM_XVIRTUALSCREEN),
        gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN),
        gsm(SM_CYVIRTUALSCREEN),
    )


# ============================================================
# グローバルホットキー
# keyboard ライブラリの低レベルフックはキーロガーと同じ仕組みのため、
# PyInstaller で固めると高確率でウイルス対策ソフトに誤検知される。
# Windows 標準の RegisterHotKey なら同じことが誤検知なしでできる。
# ============================================================
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
VK_F8 = 0x77

_user32 = ctypes.windll.user32
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_uint]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                       ctypes.c_void_p, ctypes.c_void_p]


class GlobalHotkey:
    """専用スレッドでメッセージループを回し、押下を通知する。"""

    HOTKEY_ID = 1

    def __init__(self, vk, on_press):
        self._vk = vk
        self._on_press = on_press
        self._thread = None
        self._tid = None
        self._ready = threading.Event()
        self._ok = False

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """登録に成功したら True。他アプリが同じキーを握っていると False。"""
        if self.active:
            return True
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        if not self._ok:
            self._thread = None
        return self._ok

    def _run(self):
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, self.HOTKEY_ID,
                                      MOD_NOREPEAT, self._vk):
            self._ready.set()
            return
        self._ok = True
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._on_press()
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            _user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self):
        if self.active and self._tid:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, None, None)
            self._thread.join(timeout=1.5)
        self._thread = None
        self._tid = None
        self._ok = False


# ============================================================
# 用紙サイズ（mm）
# ============================================================
A4_MM = (210.0, 297.0)
A3_LAND_MM = (420.0, 297.0)
MM_PER_INCH = 25.4

INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')

# ============================================================
# 範囲選択オーバーレイの見た目
# ウィンドウ全体に alpha がかかるため、地色を明るくすると
# 文字とのコントラストが alpha 倍に潰れて読めなくなる。
# 地色は暗く、alpha はやや高めにするのが読みやすい。
# ============================================================
OVERLAY_ALPHA = 0.45        # 0.3 → 0.45（下の画面は十分透けて見える）
OVERLAY_BG = "#141414"      # grey → ほぼ黒
OVERLAY_RECT = "#ff3b30"    # 選択枠の色
HINT_FONT = ("Meiryo UI", 14, "bold")
SIZE_FONT = ("Consolas", 12, "bold")


def sanitize_filename(name, fallback="output"):
    name = INVALID_CHARS.sub("_", (name or "").strip())
    name = name.rstrip(" .")
    return name or fallback


class PDFCameraApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PDF Camera + Capture")
        self.root.geometry("520x760")
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.save_dir = tk.StringVar(value=os.getcwd())
        self.file_name = tk.StringVar(value="output")
        self.resolution = tk.StringVar(value="300")
        self.is_single = tk.BooleanVar(value=False)
        self.gamma_val = tk.DoubleVar(value=1.0)

        self.area = None
        self.spool_dir = None      # キャプチャの一時保存先
        self.page_count = 0
        self.last_base_name = ""
        self.file_counter = 1

        # ホットキーは別スレッドで発火するので、キュー経由でメインスレッドに渡す。
        # ワーカースレッドから Tk ウィジェットを触ると不定期にクラッシュする。
        self.events = queue.Queue()
        self.hotkey = GlobalHotkey(VK_F8, lambda: self.events.put("hotkey"))
        self.hotkey_mode = None    # "catalog" / "single" / None

        self.sample_image = self.create_sample_image()
        self.setup_ui()
        self.update_preview()
        self.root.after(50, self._drain_events)

    # ----------------------------------------------------------
    # UI
    # ----------------------------------------------------------
    def setup_ui(self):
        pad = {"padx": 20, "pady": 5}

        f1 = tk.LabelFrame(self.root, text="保存フォルダ", padx=10, pady=5)
        f1.pack(fill="x", **pad)
        tk.Entry(f1, textvariable=self.save_dir, width=35).pack(side="left")
        tk.Button(f1, text="参照", command=self.browse_dir).pack(side="left")

        f2 = tk.LabelFrame(self.root, text="ファイル名", padx=10, pady=5)
        f2.pack(fill="x", **pad)
        tk.Entry(f2, textvariable=self.file_name, width=30).pack(side="left")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        self.tab1 = tk.Frame(self.notebook, bg="#f8f9fa")
        self.tab2 = tk.Frame(self.notebook, bg="#e0f7fa")
        self.notebook.add(self.tab1, text="カタログカメラ")
        self.notebook.add(self.tab2, text="画面キャプチャ")

        self.setup_catalog_tab()
        self.setup_capture_tab()

    def browse_dir(self):
        # キャンセル時は空文字が返る。そのまま set すると保存先が消える。
        d = filedialog.askdirectory(initialdir=self.save_dir.get() or os.getcwd())
        if d:
            self.save_dir.set(d)

    def setup_catalog_tab(self):
        pad = {"padx": 10, "pady": 5}

        f3 = tk.LabelFrame(self.tab1, text="設定")
        f3.pack(fill="x", **pad)
        tk.Label(f3, text="解像度の上限").grid(row=0, column=0, padx=4)
        ttk.Combobox(f3, textvariable=self.resolution,
                     values=["200", "300", "400"], width=5,
                     state="readonly").grid(row=0, column=1)
        tk.Label(f3, text="dpi").grid(row=0, column=2)
        tk.Checkbutton(f3, text="見開き分割",
                       variable=self.is_single).grid(row=0, column=3, padx=10)

        f4 = tk.LabelFrame(self.tab1, text="ガンマ補正  ← 明るく / 暗く →")
        f4.pack(fill="x", **pad)
        tk.Scale(f4, from_=0.4, to=2.5, resolution=0.05, orient="horizontal",
                 variable=self.gamma_val, command=self.update_preview).pack(fill="x")

        self.preview_label = tk.Label(self.tab1)
        self.preview_label.pack(pady=10)

        self.btn_main = tk.Button(
            self.tab1, text="範囲を指定して開始 (Enter確定)",
            bg="#28a745", fg="white", font=("MS Gothic", 12, "bold"),
            command=self.start_catalog_workflow, height=2)
        self.btn_main.pack(fill="x", padx=20, pady=10)

    def setup_capture_tab(self):
        f = tk.LabelFrame(self.tab2, text="単発キャプチャ（F8 で PNG 保存）")
        f.pack(fill="x", padx=20, pady=40)

        self.btn_single = tk.Button(f, text="範囲指定を開始", bg="#007bff",
                                    fg="white",
                                    command=self.start_single_capture_workflow)
        self.btn_single.pack(fill="x", pady=(20, 5))

        self.btn_single_stop = tk.Button(f, text="停止", state="disabled",
                                         command=self.stop_single_capture)
        self.btn_single_stop.pack(fill="x", pady=(0, 20))

        self.single_status = tk.Label(f, text="停止中", bg="#e0f7fa")
        self.single_status.pack()

    def on_tab_changed(self, _e=None):
        # タブを切り替えたら単発モードは解除しておく（ホットキーの取り残し防止）
        if self.hotkey_mode == "single":
            self.stop_single_capture()

    # ----------------------------------------------------------
    # ホットキーイベントの受け口（メインスレッド）
    # ----------------------------------------------------------
    def _drain_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "hotkey":
                    if self.hotkey_mode == "catalog":
                        self.capture_action()
                    elif self.hotkey_mode == "single":
                        self.quick_save_action()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    # ----------------------------------------------------------
    # 範囲選択オーバーレイ（Enter確定）
    # ----------------------------------------------------------
    def start_selection_overlay(self, callback):
        self.root.withdraw()
        self.sel_win = tk.Toplevel()
        self.sel_win.overrideredirect(True)
        self.sel_win.attributes("-alpha", OVERLAY_ALPHA)
        self.sel_win.attributes("-topmost", True)

        # 仮想デスクトップ全体を覆う（サブモニタ上のブラウザも選択できる）
        vx, vy, vw, vh = get_virtual_screen()
        self.sel_win.geometry(f"{vw}x{vh}+{vx}+{vy}")

        self.canvas = tk.Canvas(self.sel_win, cursor="cross", bg=OVERLAY_BG,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # 案内文は黒帯の上に置く。地色のグレーに白文字を直接乗せると、
        # alpha が効いた結果コントラストがほとんど残らない。
        hint = "ドラッグで範囲選択  →  [Enter] 確定  /  [Esc] 戻る"
        tid = self.canvas.create_text(30, 28, text=hint, fill="#ffffff",
                                      anchor="nw", font=HINT_FONT)
        x1, y1, x2, y2 = self.canvas.bbox(tid)
        self.canvas.create_rectangle(x1 - 14, y1 - 10, x2 + 14, y2 + 10,
                                     fill="#000000", outline="#ffffff", width=1)
        self.canvas.tag_raise(tid)

        self.start_x = self.start_y = 0
        self.cur_x = self.cur_y = 0
        self.rect = None
        self.size_bg = None
        self.size_txt = None

        def show_size():
            """選択サイズを枠のそばに表示する。毎回同じ大きさで撮りたいときの目安。"""
            w = abs(self.cur_x - self.start_x)
            h = abs(self.cur_y - self.start_y)
            label = f"{w} x {h} px"
            lx = min(self.start_x, self.cur_x)
            ly = min(self.start_y, self.cur_y) - 30
            if ly < 5:                       # 上端に寄ったら枠の内側に逃がす
                ly = min(self.start_y, self.cur_y) + 8
            if self.size_txt is None:
                self.size_bg = self.canvas.create_rectangle(
                    0, 0, 0, 0, fill="#000000", outline="")
                self.size_txt = self.canvas.create_text(
                    0, 0, text="", fill="#ffe600", anchor="nw", font=SIZE_FONT)
            self.canvas.itemconfig(self.size_txt, text=label)
            self.canvas.coords(self.size_txt, lx + 6, ly + 3)
            bx1, by1, bx2, by2 = self.canvas.bbox(self.size_txt)
            self.canvas.coords(self.size_bg, bx1 - 6, by1 - 3, bx2 + 6, by2 + 3)
            self.canvas.tag_raise(self.size_bg)
            self.canvas.tag_raise(self.size_txt)

        def on_press(e):
            self.start_x, self.start_y = e.x, e.y
            self.cur_x, self.cur_y = e.x, e.y
            if self.rect:
                self.canvas.delete(self.rect)
            self.rect = self.canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline=OVERLAY_RECT, width=2)
            show_size()

        def on_move(e):
            if not self.rect:
                return
            self.cur_x, self.cur_y = e.x, e.y
            self.canvas.coords(self.rect, self.start_x, self.start_y, e.x, e.y)
            show_size()

        def on_confirm(_e):
            if not self.rect:
                return
            left = min(self.start_x, self.cur_x)
            top = min(self.start_y, self.cur_y)
            right = max(self.start_x, self.cur_x)
            bottom = max(self.start_y, self.cur_y)
            if right - left < 10 or bottom - top < 10:
                return  # 誤クリックで極小範囲になるのを防ぐ
            rx = self.sel_win.winfo_rootx()
            ry = self.sel_win.winfo_rooty()
            self.area = (rx + left, ry + top, rx + right, ry + bottom)
            self.sel_win.destroy()
            callback()

        def on_cancel(_e):
            self.sel_win.destroy()
            self.root.deiconify()

        self.canvas.bind("<ButtonPress-1>", on_press)
        self.canvas.bind("<B1-Motion>", on_move)
        self.sel_win.bind("<Return>", on_confirm)
        self.sel_win.bind("<KP_Enter>", on_confirm)
        self.sel_win.bind("<Escape>", on_cancel)

        # overrideredirect のウィンドウは自動でフォーカスが来ないことがあり、
        # Enter / Esc が効かなくなる。明示的に奪っておく。
        self.sel_win.update_idletasks()
        self.sel_win.focus_force()
        self.canvas.focus_set()

    # ----------------------------------------------------------
    # カタログ撮影フロー
    # ----------------------------------------------------------
    def start_catalog_workflow(self):
        self.start_selection_overlay(self.open_rec_control)

    def open_rec_control(self):
        self.cleanup_spool()
        self.spool_dir = tempfile.mkdtemp(prefix="pdf_cam_")
        self.page_count = 0

        self.rec_win = tk.Toplevel()
        self.rec_win.geometry("260x130+10+10")
        self.rec_win.attributes("-topmost", True)
        self.rec_win.overrideredirect(True)
        self.rec_win.config(bg="black")

        self.count_label = tk.Label(
            self.rec_win, text="● REC中 (F8で撮影)\n現在: 0枚",
            fg="red", bg="black", font=("", 12, "bold"))
        self.count_label.pack(pady=10)
        tk.Button(self.rec_win, text="PDFを保存して終了",
                  command=self.finish_catalog).pack(fill="x", padx=10)
        tk.Button(self.rec_win, text="破棄して戻る",
                  command=self.cancel_catalog).pack(fill="x", padx=10, pady=(4, 0))

        self.hotkey_mode = "catalog"
        if not self.hotkey.start():
            self.hotkey_mode = None
            self.rec_win.destroy()
            self.cleanup_spool()
            self.root.deiconify()
            messagebox.showerror(
                "ホットキー登録失敗",
                "F8 を他のアプリが使用中のため登録できませんでした。\n"
                "常駐ソフトを終了してから再度お試しください。")
            return
        winsound.Beep(1000, 200)

    def capture_action(self):
        """メインスレッドから呼ばれる。撮ったそばからディスクに書き出す。"""
        try:
            img = ImageGrab.grab(bbox=self.area, all_screens=True).convert("RGB")
        except Exception as e:
            winsound.Beep(400, 300)
            print(f"キャプチャ失敗: {e}")
            return

        # メモリに溜め込むと 100 ページ規模で数 GB になり落ちる。
        # 1 枚ごとに保存して、PDF 化のときに読み直す。
        if self.is_single.get():
            w, h = img.size
            parts = [img.crop((0, 0, w // 2, h)), img.crop((w // 2, 0, w, h))]
        else:
            parts = [img]

        for part in parts:
            path = os.path.join(self.spool_dir, f"page_{self.page_count:04d}.png")
            part.save(path, "PNG")
            self.page_count += 1

        winsound.MessageBeep()
        self.count_label.config(
            text=f"● REC中 (F8で撮影)\n現在: {self.page_count}枚")

    def cancel_catalog(self):
        self.stop_catalog_mode()
        self.cleanup_spool()
        self.root.deiconify()

    def stop_catalog_mode(self):
        self.hotkey_mode = None
        self.hotkey.stop()
        if hasattr(self, "rec_win") and self.rec_win.winfo_exists():
            self.rec_win.destroy()

    def finish_catalog(self):
        self.stop_catalog_mode()

        pages = sorted(
            os.path.join(self.spool_dir, f)
            for f in os.listdir(self.spool_dir) if f.endswith(".png")
        ) if self.spool_dir else []

        if not pages:
            messagebox.showwarning("キャンセル", "キャプチャされた画像がありません。")
            self.cleanup_spool()
            self.root.deiconify()
            return

        try:
            dpi_cap = int(self.resolution.get())
        except ValueError:
            dpi_cap = 300

        page_mm = A4_MM if self.is_single.get() else A3_LAND_MM
        page_w_in = page_mm[0] / MM_PER_INCH
        page_h_in = page_mm[1] / MM_PER_INCH
        max_w = int(page_w_in * dpi_cap)
        max_h = int(page_h_in * dpi_cap)
        page_aspect = page_mm[0] / page_mm[1]

        g = self.gamma_val.get()
        lut = [max(0, min(255, int(((i / 255.0) ** g) * 255))) for i in range(256)]

        base_name = sanitize_filename(self.file_name.get())
        if base_name.lower().endswith(".pdf"):
            base_name = base_name[:-4]
        full_path = self._resolve_pdf_path(base_name)

        jpeg_dir = tempfile.mkdtemp(prefix="pdf_cam_out_")
        try:
            jpegs = []
            for i, src in enumerate(pages):
                with Image.open(src) as im:
                    im = im.convert("RGB")

                    # 1. ガンマ補正
                    im = im.point(lut * 3)

                    # 2. 上限を超えるときだけ縮小。拡大はしない。
                    #    画面キャプチャを 300dpi まで引き伸ばしても情報は増えず、
                    #    ファイルサイズだけ跳ね上がる。
                    scale = min(1.0, max_w / im.width, max_h / im.height)
                    if scale < 1.0:
                        im = im.resize(
                            (max(1, round(im.width * scale)),
                             max(1, round(im.height * scale))),
                            Image.LANCZOS)

                    # 3. 用紙の縦横比に合うよう白で余白を足す（拡大は伴わない）
                    if im.width / im.height > page_aspect:
                        box = (im.width, round(im.width / page_aspect))
                    else:
                        box = (round(im.height * page_aspect), im.height)
                    final = ImageOps.pad(im, box, method=Image.LANCZOS,
                                         color=(255, 255, 255))

                    out_dpi = max(1, round(final.width / page_w_in))
                    dst = os.path.join(jpeg_dir, f"page_{i:04d}.jpg")
                    final.save(dst, "JPEG", quality=92,
                               dpi=(out_dpi, out_dpi), optimize=True)
                    jpegs.append(dst)

            layout_fun = img2pdf.get_layout_fun(
                img2pdf.mm_to_pt(page_mm[0], page_mm[1]))
            with open(full_path, "wb") as f:
                f.write(img2pdf.convert(jpegs, layout_fun=layout_fun))

            messagebox.showinfo(
                "完了", f"PDF を作成しました（{len(jpegs)}ページ）:\n{full_path}")

        except Exception as e:
            messagebox.showerror("エラー", f"変換中にエラーが発生しました:\n{e}")
        finally:
            shutil.rmtree(jpeg_dir, ignore_errors=True)
            self.cleanup_spool()
            self.root.deiconify()

    def cleanup_spool(self):
        if self.spool_dir:
            shutil.rmtree(self.spool_dir, ignore_errors=True)
            self.spool_dir = None
        self.page_count = 0

    def _resolve_pdf_path(self, base_name):
        """同名 PDF があれば連番を付けて回避する"""
        save_dir = self.save_dir.get() or os.getcwd()
        os.makedirs(save_dir, exist_ok=True)
        candidate = os.path.join(save_dir, f"{base_name}.pdf")
        if not os.path.exists(candidate):
            return candidate
        counter = 1
        while True:
            candidate = os.path.join(save_dir, f"{base_name}_{counter:03d}.pdf")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    # ----------------------------------------------------------
    # 単発キャプチャフロー
    # ----------------------------------------------------------
    def start_single_capture_workflow(self):
        def activate():
            self.root.deiconify()
            self.hotkey_mode = "single"
            if not self.hotkey.start():
                self.hotkey_mode = None
                messagebox.showerror(
                    "ホットキー登録失敗",
                    "F8 を他のアプリが使用中のため登録できませんでした。")
                return
            self.btn_single.config(state="disabled")
            self.btn_single_stop.config(state="normal")
            self.single_status.config(text="待機中 — F8 で PNG 保存")

        self.start_selection_overlay(activate)

    def stop_single_capture(self):
        self.hotkey_mode = None
        self.hotkey.stop()
        self.btn_single.config(state="normal")
        self.btn_single_stop.config(state="disabled")
        self.single_status.config(text="停止中")

    def quick_save_action(self):
        try:
            img = ImageGrab.grab(bbox=self.area, all_screens=True).convert("RGB")
            path = self.get_next_filename(".png")
            img.save(path, "PNG")
            winsound.Beep(800, 100)
            self.single_status.config(text=f"保存: {os.path.basename(path)}")
        except Exception as e:
            winsound.Beep(400, 300)
            self.single_status.config(text=f"失敗: {e}")

    def get_next_filename(self, ext):
        base = sanitize_filename(self.file_name.get())
        if base != self.last_base_name:
            self.file_counter = 1
            self.last_base_name = base
        save_dir = self.save_dir.get() or os.getcwd()
        os.makedirs(save_dir, exist_ok=True)
        while True:
            full_path = os.path.join(
                save_dir, f"{base}_{self.file_counter:03}{ext}")
            if not os.path.exists(full_path):
                return full_path
            self.file_counter += 1

    # ----------------------------------------------------------
    # プレビュー
    # ----------------------------------------------------------
    def create_sample_image(self):
        """ガンマスライダー確認用のサンプル画像"""
        base_gray = 248
        img = Image.new("RGB", (400, 250),
                        color=(base_gray, base_gray, base_gray))
        d = ImageDraw.Draw(img)
        for i in range(400):
            g_val = 255 - (i * 255 // 400)
            d.line([(i, 150), (i, 250)], fill=(g_val, g_val, g_val))
        d.rectangle([20, 20, 150, 100], fill=(160, 160, 160))
        d.text((170, 30), "GRAY PREVIEW", fill=(0, 0, 0))
        d.text((170, 60), f"Base Background: {base_gray}", fill=(100, 100, 100))
        d.text((170, 90), "Tone Curve Simulation", fill=(150, 150, 150))
        return img

    def update_preview(self, _e=None):
        g = self.gamma_val.get()
        lut = [max(0, min(255, int(((i / 255.0) ** g) * 255))) for i in range(256)]
        adjusted = self.sample_image.point(lut * 3)
        self.tk_preview = ImageTk.PhotoImage(adjusted)
        self.preview_label.config(image=self.tk_preview)

    # ----------------------------------------------------------
    def on_close(self):
        self.hotkey_mode = None
        self.hotkey.stop()
        self.cleanup_spool()
        self.root.destroy()


if __name__ == "__main__":
    PDFCameraApp().root.mainloop()