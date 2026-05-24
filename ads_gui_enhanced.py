# ads_gui_enhanced.py
# Улучшенная версия GUI для ADS Video Sync
# Требуется: pip install ttkbootstrap tkdnd

import sys
import os
import threading
import subprocess
import json
import re
from pathlib import Path
from datetime import datetime

# Глобальные импорты с обработкой ошибок
try:
    import ttkbootstrap as ttkb
    from ttkbootstrap.constants import *
    USE_TTKBOOTSTRAP = True
except ImportError:
    USE_TTKBOOTSTRAP = False
    print("⚠️ ttkbootstrap не установлен. Запуск в стандартном режиме.")
    print("   Для современного дизайна: pip install ttkbootstrap")

try:
    import tkinter.dnd as tkdnd
    USE_DND = True
except ImportError:
    USE_DND = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

def launch_enhanced_gui():
    """Запуск улучшенного GUI с современным дизайном"""
    import json
    
    class EnhancedApp:
        def __init__(self, root):
            self.root = root
            self.root.title("🎬 ADS Video Sync - Enhanced GUI")
            
            if USE_TTKBOOTSTRAP:
                self.style = ttkb.Style(theme='darkly')
                self.root.geometry("1100x850")
            else:
                self.root.geometry("1000x750")
            
            # Определение пути запуска
            if getattr(sys, 'frozen', False):
                self.runner = [sys.executable]
            else:
                self.runner = [sys.executable, os.path.abspath(__file__)]
            
            self.process = None
            
            # Пресеты настроек
            self.presets = {
                "Default": {"auto_sync": True, "deep_scan": True, "precise": True},
                "Fast": {"auto_sync": True, "deep_scan": False, "precise": False},
                "Quality": {"auto_sync": True, "deep_scan": True, "precise": True},
                "YouTube 1080p": {"auto_sync": True, "deep_scan": True, "precise": True},
                "Instagram Reels": {"auto_sync": True, "deep_scan": False, "precise": False},
                "TikTok": {"auto_sync": True, "deep_scan": False, "precise": False},
            }
            
            self.setup_ui()
        
        def setup_ui(self):
            """Создание интерфейса"""
            main_frame = ttk.Frame(self.root, padding=10)
            main_frame.pack(fill=BOTH, expand=True)
            
            # Заголовок
            title_label = ttk.Label(
                main_frame, 
                text="🎬 ADS Video Sync", 
                font=('Arial', 16, 'bold')
            )
            title_label.pack(pady=(0, 10))
            
            # Секция файлов
            frm_files = ttk.LabelFrame(main_frame, text="📁 Файлы для обработки", padding=10)
            frm_files.pack(fill='x', pady=(0, 10))
            
            self.source_var = tk.StringVar()
            self.target_var = tk.StringVar()
            self.workdir_var = tk.StringVar(
                value=os.path.join(os.path.expanduser("~"), "Desktop", "ads_workdir")
            )
            
            # Source файл
            ttk.Label(frm_files, text="Source (Неправильный тайминг):").grid(
                row=0, column=0, sticky='w', pady=4
            )
            self.source_entry = ttk.Entry(frm_files, textvariable=self.source_var, width=70)
            self.source_entry.grid(row=0, column=1, padx=5, pady=4)
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse(self.source_var)).grid(
                row=0, column=2, pady=4
            )
            
            # Target файл
            ttk.Label(frm_files, text="Target (Правильный тайминг):").grid(
                row=1, column=0, sticky='w', pady=4
            )
            self.target_entry = ttk.Entry(frm_files, textvariable=self.target_var, width=70)
            self.target_entry.grid(row=1, column=1, padx=5, pady=4)
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse(self.target_var)).grid(
                row=1, column=2, pady=4
            )
            
            # Рабочая папка
            ttk.Label(frm_files, text="Рабочая папка:").grid(
                row=2, column=0, sticky='w', pady=4
            )
            ttk.Entry(frm_files, textvariable=self.workdir_var, width=70).grid(
                row=2, column=1, padx=5, pady=4
            )
            ttk.Button(frm_files, text="Обзор", command=lambda: self.browse_dir(self.workdir_var)).grid(
                row=2, column=2, pady=4
            )
            
            # Подсказка про Drag & Drop
            if USE_DND:
                dnd_label = ttk.Label(
                    frm_files, 
                    text="💡 Совет: Перетащите видеофайлы прямо в поля выше", 
                    font=('Arial', 9, 'italic'),
                    foreground='gray'
                )
                dnd_label.grid(row=3, column=0, columnspan=3, sticky='w', pady=(5, 0))
            
            # Секция пресетов
            frm_presets = ttk.LabelFrame(main_frame, text="⚡ Быстрые пресеты", padding=10)
            frm_presets.pack(fill='x', pady=(0, 10))
            
            preset_frame = ttk.Frame(frm_presets)
            preset_frame.pack(fill='x')
            
            ttk.Label(preset_frame, text="Выберите пресет:").pack(side='left', padx=(0, 10))
            self.preset_var = tk.StringVar(value="Default")
            preset_combo = ttk.Combobox(
                preset_frame, 
                textvariable=self.preset_var, 
                values=list(self.presets.keys()), 
                width=25, 
                state='readonly'
            )
            preset_combo.pack(side='left', padx=(0, 10))
            preset_combo.bind('<<ComboboxSelected>>', lambda e: self.apply_preset())
            
            ttk.Button(preset_frame, text="Применить", command=self.apply_preset).pack(side='left')
            
            # Секция опций
            frm_opts = ttk.LabelFrame(main_frame, text="⚙️ Опции обработки", padding=10)
            frm_opts.pack(fill='x', pady=(0, 10))
            
            self.auto_sync = tk.BooleanVar(value=True)
            self.deep_scan = tk.BooleanVar(value=True)
            self.precise = tk.BooleanVar(value=True)
            
            opts_frame = ttk.Frame(frm_opts)
            opts_frame.pack(fill='x')
            
            ttk.Checkbutton(
                opts_frame, 
                text="Auto Sync (Автоматическая синхронизация)", 
                variable=self.auto_sync
            ).grid(row=0, column=0, padx=10, pady=5, sticky='w')
            
            ttk.Checkbutton(
                opts_frame, 
                text="Deep Scan Edges (Глубокий поиск границ)", 
                variable=self.deep_scan
            ).grid(row=0, column=1, padx=10, pady=5, sticky='w')
            
            ttk.Checkbutton(
                opts_frame, 
                text="Precise Scene Detect (Точное определение сцен)", 
                variable=self.precise
            ).grid(row=0, column=2, padx=10, pady=5, sticky='w')
            
            # Кнопки управления
            frm_ctrl = ttk.Frame(main_frame, padding=10)
            frm_ctrl.pack(fill='x', pady=(0, 10))
            
            btn_style = {'bootstyle': PRIMARY} if USE_TTKBOOTSTRAP else {}
            self.btn_scan = ttk.Button(
                frm_ctrl, 
                text="1. 🔍 Сканировать и создать превью", 
                command=self.run_scan, 
                **btn_style
            )
            self.btn_scan.pack(side='left', padx=5)
            
            btn_style_fin = {'bootstyle': SUCCESS} if USE_TTKBOOTSTRAP else {}
            self.btn_finalize = ttk.Button(
                frm_ctrl, 
                text="2. 🎬 Финализировать (Собрать видео)", 
                command=self.run_finalize, 
                state='disabled',
                **btn_style_fin
            )
            self.btn_finalize.pack(side='left', padx=5)
            
            self.btn_open = ttk.Button(
                frm_ctrl, 
                text="📂 Открыть папку", 
                command=self.open_workdir
            )
            self.btn_open.pack(side='left', padx=5)
            
            btn_style_rep = {'bootstyle': INFO} if USE_TTKBOOTSTRAP else {}
            self.btn_report = ttk.Button(
                frm_ctrl, 
                text="📊 HTML отчёт", 
                command=self.open_html_report,
                **btn_style_rep
            )
            self.btn_report.pack(side='left', padx=5)
            
            btn_style_stop = {'bootstyle': DANGER} if USE_TTKBOOTSTRAP else {}
            self.btn_stop = ttk.Button(
                frm_ctrl, 
                text="⏹ Остановить", 
                command=self.stop_process, 
                state='disabled',
                **btn_style_stop
            )
            self.btn_stop.pack(side='right', padx=5)
            
            # Прогресс бар
            progress_frame = ttk.Frame(main_frame)
            progress_frame.pack(fill='x', pady=(0, 5))
            
            prog_style = {'bootstyle': "success-striped"} if USE_TTKBOOTSTRAP else {}
            self.progress = ttk.Progressbar(
                progress_frame, 
                mode='determinate', 
                length=400,
                **prog_style
            )
            self.progress.pack(fill='x', side='top')
            
            self.status_var = tk.StringVar(value="✅ Готов к работе. Выберите файлы и нажмите 'Сканировать'")
            stat_style = {'bootstyle': "info"} if USE_TTKBOOTSTRAP else {}
            self.status_label = ttk.Label(
                progress_frame, 
                textvariable=self.status_var, 
                wraplength=900,
                **stat_style
            )
            self.status_label.pack(fill='x', side='top', pady=(5, 0))
            
            # Лог
            log_frame = ttk.LabelFrame(main_frame, text="📝 Журнал операций", padding=5)
            log_frame.pack(fill=BOTH, expand=True)
            
            self.log_text = scrolledtext.ScrolledText(
                log_frame, 
                height=12, 
                wrap='word', 
                font=("Consolas", 9)
            )
            self.log_text.pack(fill=BOTH, expand=True)
            
            # Цветовая схема логов
            self.log_text.tag_config("info", foreground="#2196F3")
            self.log_text.tag_config("success", foreground="#4CAF50")
            self.log_text.tag_config("error", foreground="#F44336")
            self.log_text.tag_config("warning", foreground="#FF9800")
            self.log_text.tag_config("progress", foreground="#9C27B0")
            
            # Настройка Drag & Drop
            if USE_DND:
                self._setup_drag_drop()
            
            # Приветственное сообщение
            self.log("🎬 ADS Video Sync Enhanced GUI запущен!", "info")
            self.log("💡 Перетащите видеофайлы или используйте кнопки 'Обзор'", "info")
            if USE_TTKBOOTSTRAP:
                self.log("✨ Используется современный дизайн (ttkbootstrap)", "success")
            else:
                self.log("⚙️ Стандартный режим интерфейса", "warning")
        
        def _setup_drag_drop(self):
            """Настройка Drag & Drop для окон"""
            try:
                self.root.drop_target_register(tk.DND_FILES)
                self.root.dnd_bind('<<Drop>>', self.on_drop)
                
                # Также добавляем обработчики для полей ввода
                for entry in [self.source_entry, self.target_entry]:
                    entry.drop_target_register(tk.DND_FILES)
                    entry.dnd_bind('<<Drop>>', lambda e, ent=entry: self.on_entry_drop(e, ent))
            except Exception as e:
                self.log(f"⚠️ Drag & Drop недоступен: {e}", "warning")
        
        def on_drop(self, event):
            """Обработка перетаскивания файлов на окно"""
            files = event.data.split()
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')
            
            for file in files:
                file = file.replace('{', '').replace('}', '')
                if file.lower().endswith(video_extensions):
                    if not self.source_var.get():
                        self.source_var.set(file)
                        self.log(f"📥 Добавлен Source: {os.path.basename(file)}", "info")
                    elif not self.target_var.get():
                        self.target_var.set(file)
                        self.log(f"📥 Добавлен Target: {os.path.basename(file)}", "info")
                    else:
                        self.log(f"⚠️ Файл пропущен (оба поля заняты): {os.path.basename(file)}", "warning")
                        return
                
                self.log(f"✅ Файлы добавлены. Нажмите 'Сканировать' для начала.", "success")
        
        def on_entry_drop(self, event, entry):
            """Обработка перетаскивания на конкретное поле"""
            files = event.data.split()
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')
            
            for file in files:
                file = file.replace('{', '').replace('}', '')
                if file.lower().endswith(video_extensions):
                    if entry == self.source_entry:
                        self.source_var.set(file)
                        self.log(f"📥 Source: {os.path.basename(file)}", "info")
                    elif entry == self.target_entry:
                        self.target_var.set(file)
                        self.log(f"📥 Target: {os.path.basename(file)}", "info")
        
        def apply_preset(self):
            """Применение выбранного пресета"""
            preset_name = self.preset_var.get()
            if preset_name in self.presets:
                p = self.presets[preset_name]
                self.auto_sync.set(p["auto_sync"])
                self.deep_scan.set(p["deep_scan"])
                self.precise.set(p["precise"])
                self.log(f"⚡ Применён пресет: {preset_name}", "info")
                
                # Показываем описание пресета
                descriptions = {
                    "Default": "Стандартные настройки для большинства задач",
                    "Fast": "Быстрая обработка с минимальным качеством",
                    "Quality": "Максимальное качество с глубоким анализом",
                    "YouTube 1080p": "Оптимально для YouTube 1080p",
                    "Instagram Reels": "Для коротких вертикальных видео",
                    "TikTok": "Для TikTok и подобных платформ",
                }
                if preset_name in descriptions:
                    self.log(f"   💡 {descriptions[preset_name]}", "info")
        
        def browse(self, var):
            """Выбор файла через диалог"""
            path = filedialog.askopenfilename(
                title="Выберите видеофайл",
                filetypes=[
                    ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.m4v"),
                    ("All files", "*.*")
                ]
            )
            if path:
                var.set(path)
                self.log(f"📁 Выбран файл: {os.path.basename(path)}", "info")
        
        def browse_dir(self, var):
            """Выбор папки через диалог"""
            path = filedialog.askdirectory(title="Выберите рабочую папку")
            if path:
                var.set(path)
                self.log(f"📂 Рабочая папка: {path}", "info")
        
        def open_workdir(self):
            """Открытие рабочей папки"""
            workdir = self.workdir_var.get()
            if os.path.exists(workdir):
                if os.name == 'nt':
                    os.startfile(workdir)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', workdir])
                else:
                    subprocess.run(['xdg-open', workdir])
            else:
                messagebox.showwarning("Папка не найдена", f"Папка не существует:\n{workdir}")
        
        def open_html_report(self):
            """Открытие HTML отчёта"""
            report_path = os.path.join(self.workdir_var.get(), "preview_report.html")
            if os.path.exists(report_path):
                if os.name == 'nt':
                    os.startfile(report_path)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', report_path])
                else:
                    subprocess.run(['xdg-open', report_path])
            else:
                messagebox.showwarning(
                    "Нет отчёта",
                    "HTML-отчёт ещё не создан.\nСначала нажмите '1. 🔍 Сканировать'."
                )
        
        def log(self, msg, level="info"):
            """Добавление сообщения в лог"""
            timestamp = time.strftime("%H:%M:%S")
            formatted_msg = f"[{timestamp}] {msg}"
            self.log_text.insert('end', formatted_msg + '\n', level)
            self.log_text.see('end')
        
        def build_cmd(self, phase):
            """Построение команды для запуска"""
            cmd = self.runner + [
                "--phase", phase, 
                "--workdir", self.workdir_var.get(),
                "-sp", self.source_var.get(), 
                "-tp", self.target_var.get(),
                "--progress", 
                "-st", "2.0", 
                "-lt", "5.0",
                "--deep-search-window", "1.3", 
                "--deep-miss-forward", "35",
                "--deep-hash-threshold", "18", 
                "--deep-region-mode", "multi"
            ]
            
            if self.auto_sync.get(): 
                cmd.append("--auto-sync")
            if not self.deep_scan.get(): 
                cmd.append("--no-deep-scan-edges")
            if self.precise.get(): 
                cmd.append("--use-precise-scene-detect")
            
            return cmd
        
        def run_process(self, phase):
            """Запуск процесса обработки"""
            if not self.source_var.get() or not self.target_var.get():
                messagebox.showerror(
                    "Ошибка", 
                    "Выберите Source и Target файлы!\n\n"
                    "Source - файл с неправильным таймингом\n"
                    "Target - файл с правильным таймингом"
                )
                return
            
            # Блокировка кнопок
            self.btn_scan.config(state='disabled')
            self.btn_finalize.config(state='disabled')
            self.btn_stop.config(state='normal')
            self.progress['value'] = 0
            
            self.log(f"--- 🚀 Запуск фазы: {phase} ---", "info")
            cmd = self.build_cmd(phase)
            
            def target():
                try:
                    kwargs = dict(
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.STDOUT,
                        text=True, 
                        encoding='utf-8', 
                        errors='replace', 
                        bufsize=1
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
                            except: 
                                pass
                        else:
                            if line: 
                                self.root.after(0, self.log, line)
                    
                    self.process.wait()
                    rc = self.process.returncode
                    self.root.after(0, self.on_finish, phase, rc)
                    
                except Exception as e:
                    self.root.after(0, self.log, f"❌ CRITICAL ERROR: {e}", "error")
                    self.root.after(0, self.on_finish, phase, -1)
            
            threading.Thread(target=target, daemon=True).start()
        
        def update_progress(self, val, msg):
            """Обновление прогресса"""
            self.progress['value'] = val
            self.status_var.set(f"⏳ {msg}")
            if val % 10 < 1:  # Обновляем лог каждые 10%
                self.log(f"📊 Прогресс: {val:.1f}% - {msg}", "progress")
        
        def on_finish(self, phase, rc):
            """Завершение процесса"""
            self.btn_stop.config(state='disabled')
            self.process = None
            
            if rc == 0:
                self.log(f"✅ Фаза '{phase}' успешно завершена!", "success")
                self.status_var.set("✅ Готово!")
                
                if phase == "scan":
                    self.btn_finalize.config(state='normal')
                    self.btn_scan.config(state='normal')
                    messagebox.showinfo(
                        "Успех",
                        "🎉 Сканирование завершено!\n\n"
                        "📊 Нажмите '📊 HTML отчёт' чтобы просмотреть все найденные различия.\n\n"
                        "Затем нажмите '2. 🎬 Финализировать' для создания итогового видео."
                    )
                else:
                    self.btn_scan.config(state='normal')
                    messagebox.showinfo(
                        "Успех", 
                        "🎉 Финализация завершена!\n\n"
                        "Готовое видео сохранено в рабочей папке."
                    )
            else:
                self.log(f"❌ Ошибка выполнения (код {rc}).", "error")
                self.status_var.set("❌ Ошибка!")
                self.btn_scan.config(state='normal')
                if phase == "finalize": 
                    self.btn_finalize.config(state='normal')
                messagebox.showerror("Ошибка", f"Процесс завершился с ошибкой (код {rc})")
        
        def run_scan(self): 
            """Запуск сканирования"""
            self.run_process("scan")
        
        def run_finalize(self): 
            """Запуск финализации"""
            self.run_process("finalize")
        
        def stop_process(self):
            """Остановка процесса"""
            if self.process:
                if messagebox.askyesno("Подтверждение", "Остановить текущий процесс?"):
                    self.process.terminate()
                    self.log("⏹ Процесс остановлен пользователем", "warning")
                    self.btn_stop.config(state='disabled')
                    self.btn_scan.config(state='normal')
                    self.btn_finalize.config(state='normal')
    
    # Импорт time для логирования
    import time
    
    # Создание и запуск приложения
    root = tk.Tk()
    
    # Установка иконки (если есть)
    try:
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except:
        pass
    
    app = EnhancedApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_enhanced_gui()
