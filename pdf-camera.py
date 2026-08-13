import ctypes
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import ImageGrab, Image, ImageDraw, ImageTk, ImageOps
import img2pdf
import keyboard
import winsound
import tempfile

# ============================================================
# Windows専用アプリケーション
# DPI対策（マルチモニター・高解像度ディスプレイ対応）
# ============================================================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class PDFCameraApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PDF Camera + Capture")
        self.root.geometry("520x720")
        self.root.attributes("-topmost", True)

        self.save_dir = tk.StringVar(value=os.getcwd())
        self.file_name = tk.StringVar(value="output")
        self.resolution = tk.StringVar(value="300")
        self.is_single = tk.BooleanVar(value=False)
        self.gamma_val = tk.DoubleVar(value=1.0)

        self.captured_images = []
        self.area = None
        self.last_base_name = ""
        self.file_counter = 1

        self.sample_image = self.create_sample_image()
        self.setup_ui()
        self.root.after(100, self.update_preview)

    def setup_ui(self):
        pad = {'padx': 20, 'pady': 5}
        f1 = tk.LabelFrame(self.root, text="保存フォルダ", padx=10, pady=5)
        f1.pack(fill="x", **pad)
        tk.Entry(f1, textvariable=self.save_dir, width=35).pack(side="left")
        tk.Button(f1, text="参照", command=lambda: self.save_dir.set(
            filedialog.askdirectory())).pack(side="left")

        f2 = tk.LabelFrame(self.root, text="ファイル名", padx=10, pady=5)
        f2.pack(fill="x", **pad)
        tk.Entry(f2, textvariable=self.file_name, width=30).pack(side="left")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab1 = tk.Frame(self.notebook, bg="#f8f9fa")
        self.tab2 = tk.Frame(self.notebook, bg="#e0f7fa")
        self.notebook.add(self.tab1, text="カタログカメラ")
        self.notebook.add(self.tab2, text="画面キャプチャ")

        self.setup_catalog_tab()
        self.setup_capture_tab()

    def setup_catalog_tab(self):
        pad = {'padx': 10, 'pady': 5}
        f3 = tk.LabelFrame(self.tab1, text="設定")
        f3.pack(fill="x", **pad)
        tk.Label(f3, text="解像度").grid(row=0, column=0)
        ttk.Combobox(f3, textvariable=self.resolution,
                     values=["200", "300", "400"], width=5).grid(row=0, column=1)
        tk.Checkbutton(f3, text="見開き分割", variable=self.is_single).grid(row=0, column=2)

        f4 = tk.LabelFrame(self.tab1, text="ガンマ補正")
        f4.pack(fill="x", **pad)
        tk.Scale(f4, from_=1.0, to=2.5, resolution=0.05, orient="horizontal",
                 variable=self.gamma_val, command=self.update_preview).pack(fill="x")

        self.preview_label = tk.Label(self.tab1)
        self.preview_label.pack(pady=10)

        self.btn_main = tk.Button(
            self.tab1, text="範囲を指定して開始 (Enter確定)",
            bg="#28a745", fg="white", font=("MS Gothic", 12, "bold"),
            command=self.start_catalog_workflow, height=2)
        self.btn_main.pack(fill="x", padx=20, pady=10)

    def setup_capture_tab(self):
        f = tk.LabelFrame(self.tab2, text="単発キャプチャ")
        f.pack(fill="x", padx=20, pady=40)
        tk.Button(f, text="範囲指定を開始", bg="#007bff", fg="white",
                  command=self.start_single_capture_workflow).pack(fill="x", pady=20)

    # ----------------------------------------------------------
    # 範囲選択オーバーレイ（Enter確定）
    # ----------------------------------------------------------
    def start_selection_overlay(self, callback):
        self.root.withdraw()
        self.sel_win = tk.Toplevel()
        self.sel_win.attributes("-alpha", 0.3, "-topmost", True)
        self.sel_win.overrideredirect(True)

        # [FIX] state('zoomed') の代わりに画面全体のサイズを明示指定
        # → マルチモニター環境でも正しくオーバーレイが広がる
        sw = self.sel_win.winfo_screenwidth()
        sh = self.sel_win.winfo_screenheight()
        self.sel_win.geometry(f"{sw}x{sh}+0+0")

        self.canvas = tk.Canvas(
            self.sel_win, cursor="cross", bg="grey", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_text(
            20, 20,
            text="ドラッグで範囲選択 -> [Enter]で確定 / [Esc]で戻る",
            fill="white", anchor="nw", font=("", 14, "bold"))

        self.start_x = self.start_y = 0
        self.cur_x = self.cur_y = 0
        self.rect = None

        def on_press(e):
            self.start_x, self.start_y = e.x, e.y
            if self.rect:
                self.canvas.delete(self.rect)
            self.rect = self.canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="red", width=3)

        def on_move(e):
            self.cur_x, self.cur_y = e.x, e.y
            self.canvas.coords(self.rect, self.start_x,
                               self.start_y, e.x, e.y)

        def on_confirm(e):
            if not self.rect:
                return
            rx = self.sel_win.winfo_rootx()
            ry = self.sel_win.winfo_rooty()
            self.area = (
                rx + min(self.start_x, self.cur_x),
                ry + min(self.start_y, self.cur_y),
                rx + max(self.start_x, self.cur_x),
                ry + max(self.start_y, self.cur_y),
            )
            self.sel_win.destroy()
            callback()

        def on_cancel(e):
            self.sel_win.destroy()
            # ホットキーが残っていれば解除してから戻る
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self.root.deiconify()

        self.sel_win.bind("<ButtonPress-1>", on_press)
        self.sel_win.bind("<B1-Motion>", on_move)
        self.sel_win.bind("<Return>", on_confirm)
        self.sel_win.bind("<Escape>", on_cancel)

    # ----------------------------------------------------------
    # カタログ撮影フロー
    # ----------------------------------------------------------
    def start_catalog_workflow(self):
        self.start_selection_overlay(self.open_rec_control)

    def open_rec_control(self):
        self.captured_images = []
        self.rec_win = tk.Toplevel()
        self.rec_win.geometry("250x120+10+10")
        self.rec_win.attributes("-topmost", True)
        self.rec_win.overrideredirect(True)
        self.rec_win.config(bg="black")

        self.count_label = tk.Label(
            self.rec_win,
            text="● REC中 (F8で撮影)\n現在: 0枚",
            fg="red", bg="black", font=("", 12, "bold"))
        self.count_label.pack(pady=10)
        tk.Button(self.rec_win, text="PDFを保存して終了",
                  command=self.finish_catalog).pack()

        # [FIX] 既存ホットキーを確実に解除してから登録（二重登録防止）
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        keyboard.add_hotkey('f8', self.capture_action, suppress=True)
        winsound.Beep(1000, 200)

    def capture_action(self):
        img = ImageGrab.grab(bbox=self.area, all_screens=True)
        # [FIX] RGBAが返った場合に備えてRGBに統一
        img = img.convert("RGB")

        if self.is_single.get():
            w, h = img.size
            self.captured_images.append(img.crop((0, 0, w // 2, h)))
            self.captured_images.append(img.crop((w // 2, 0, w, h)))
        else:
            self.captured_images.append(img)

        winsound.MessageBeep()
        self.count_label.config(
            text=f"● REC中 (F8で撮影)\n現在: {len(self.captured_images)}枚")

    def finish_catalog(self):
        if hasattr(self, 'rec_win'):
            self.rec_win.destroy()

        # [FIX] 画像なし・ありに関わらず、まずホットキーを解除
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        if not self.captured_images:
            messagebox.showwarning("キャンセル", "キャプチャされた画像がありません。")
            self.root.deiconify()
            return

        try:
            DPI = int(self.resolution.get())
        except ValueError:
            DPI = 300

        # A4サイズ定義（インチ）
        A4_W_INCH, A4_H_INCH = 8.267, 11.692

        # 見開き分割ならA4縦、通常ならA3相当(A4横2枚分)
        if self.is_single.get():
            target_w = int(A4_W_INCH * DPI)
        else:
            target_w = int(A4_W_INCH * 2 * DPI)
        target_h = int(A4_H_INCH * DPI)

        # ガンマ補正用LUT
        g = self.gamma_val.get()
        lut = [int(((i / 255.0) ** g) * 255) for i in range(256)]

        # [FIX] PDF出力先のファイル名重複チェック（連番付与）
        save_name = self.file_name.get()
        base_name = save_name if not save_name.lower().endswith('.pdf') else save_name[:-4]
        full_path = self._resolve_pdf_path(base_name)

        with tempfile.TemporaryDirectory(prefix="pdf_cam_") as tmpdir:
            temp_files = []
            try:
                for i, img in enumerate(self.captured_images):
                    tmp_p = os.path.join(tmpdir, f"page_{i:04d}.jpg")

                    # 1. ガンマ補正
                    adj = img.point(lut * 3)

                    # 2. A4/A3サイズに収めてパディング
                    final = ImageOps.pad(
                        adj,
                        (target_w, target_h),
                        method=Image.LANCZOS,
                        color=(255, 255, 255)
                    )

                    # 3. JPEG保存（DPI埋め込み）
                    final.save(tmp_p, "JPEG", quality=95, dpi=(DPI, DPI))
                    temp_files.append(tmp_p)

                # [FIX] img2pdf にA4/A3レイアウトを明示指定してDPIを確実に反映
                if self.is_single.get():
                    layout_fun = img2pdf.get_layout_fun(
                        img2pdf.mm_to_pt(210, 297))
                else:
                    layout_fun = img2pdf.get_layout_fun(
                        img2pdf.mm_to_pt(420, 297))

                with open(full_path, "wb") as f:
                    f.write(img2pdf.convert(temp_files, layout_fun=layout_fun))

                messagebox.showinfo("完了", f"PDFを作成しました:\n{full_path}")

            except Exception as e:
                messagebox.showerror("エラー", f"変換中にエラーが発生しました:\n{e}")

        self.captured_images = []
        self.root.deiconify()

    def _resolve_pdf_path(self, base_name):
        """同名PDFが存在する場合は連番を付与して重複を回避する"""
        save_dir = self.save_dir.get()
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
            # [FIX] 既存ホットキーを解除してから登録（二重登録防止）
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            keyboard.add_hotkey('f8', self.quick_save_action, suppress=True)
            messagebox.showinfo(
                "準備完了",
                "F8キーでPNG保存します。\n(OS側のF8機能は一時的に無効化されます)")

        self.start_selection_overlay(activate)

    def quick_save_action(self):
        img = ImageGrab.grab(bbox=self.area, all_screens=True)
        # [FIX] RGBA対策
        img = img.convert("RGB")
        path = self.get_next_filename(".png")
        img.save(path, "PNG")
        winsound.Beep(800, 100)

    def get_next_filename(self, ext):
        base = self.file_name.get()
        if base != self.last_base_name:
            self.file_counter = 1
            self.last_base_name = base
        while True:
            name = f"{base}_{self.file_counter:03}{ext}"
            full_path = os.path.join(self.save_dir.get(), name)
            if not os.path.exists(full_path):
                break
            self.file_counter += 1
        return full_path

    # ----------------------------------------------------------
    # プレビュー
    # ----------------------------------------------------------
    def create_sample_image(self):
        """ガンマスライダー確認用のサンプル画像を生成"""
        base_gray = 248
        img = Image.new('RGB', (400, 250), color=(base_gray, base_gray, base_gray))
        d = ImageDraw.Draw(img)

        # 階調確認グラデーション（下半分）
        for i in range(400):
            g_val = 255 - (i * 255 // 400)
            d.line([(i, 150), (i, 250)], fill=(g_val, g_val, g_val))

        # 中間グレーのボックス
        d.rectangle([20, 20, 150, 100], fill=(160, 160, 160))

        # テキスト
        d.text((170, 30), "GRAY PREVIEW", fill=(0, 0, 0))
        d.text((170, 60), f"Base Background: {base_gray}", fill=(100, 100, 100))
        d.text((170, 90), "Tone Curve Simulation", fill=(150, 150, 150))

        return img

    def update_preview(self, e=None):
        g = self.gamma_val.get()
        lut = [int(((i / 255.0) ** g) * 255) for i in range(256)]
        adjusted = self.sample_image.point(lut * 3)
        self.tk_preview = ImageTk.PhotoImage(adjusted)
        self.preview_label.config(image=self.tk_preview)


if __name__ == "__main__":
    PDFCameraApp().root.mainloop()