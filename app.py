"""Windows desktop entry point. Only the standard library is required."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from transfer_core import default_home, scan_sessions, project_files, export_bundle, inspect_bundle, restore_bundle


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Codex Project Transfer")
        self.geometry("1040x790")
        self.minsize(900, 720)
        self.configure(bg="#eef2f6")
        self.events = queue.Queue()
        self.busy = False
        self.restored = None
        self.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("맑은 고딕", 10), background="#eef2f6")
        style.configure("TButton", padding=(12, 8))
        style.configure("TEntry", padding=7)
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white")
        style.configure("Accent.TButton", background="#176b58", foreground="white")
        header = tk.Frame(self, bg="#163b35", padx=28, pady=22)
        header.pack(fill="x")
        tk.Label(header, text="Codex Project Transfer", bg="#163b35", fg="white", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        tk.Label(header, text="프로젝트와 대화를 한 묶음으로 · Windows 미리보기 버전 0.1.1", bg="#163b35", fg="#c0ded5", font=("맑은 고딕", 10)).pack(anchor="w", pady=(7, 0))
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=22, pady=18)
        export = ttk.Frame(tabs, padding=20)
        restore = ttk.Frame(tabs, padding=20)
        about = ttk.Frame(tabs, padding=20)
        tabs.add(export, text="  1  내보내기  ")
        tabs.add(restore, text="  2  복원 · 이어가기  ")
        tabs.add(about, text="  저장 방식 · 지원 범위  ")
        self.project = tk.StringVar()
        self.home = tk.StringVar(value=str(default_home()))
        self.bundle = tk.StringVar()
        self.destination = tk.StringVar()
        self.git = tk.BooleanVar(value=True)
        self.path_row(export, "프로젝트 폴더", self.project, "folder")
        self.path_row(export, "Codex 저장소", self.home, "folder")
        opts = ttk.Frame(export); opts.pack(fill="x", pady=8)
        ttk.Checkbutton(opts, text="Git 기록 포함 (.git) · 일반 체크아웃만 지원", variable=self.git).pack(side="left")
        ttk.Button(opts, text="프로젝트 분석", command=self.analyze).pack(side="right")
        self.summary = tk.StringVar(value="프로젝트를 선택하면 연결된 대화와 파일을 확인할 수 있습니다.")
        ttk.Label(export, textvariable=self.summary, wraplength=880).pack(anchor="w", pady=8)
        self.sessions = self.tree(export, height=5)
        ttk.Label(export, text="인수인계 메모  ·  현재 목표, 완료 내용, 다음 할 일을 적어주세요.").pack(anchor="w", pady=(12, 5))
        self.note = tk.Text(export, height=4, font=("맑은 고딕", 10), relief="flat", padx=10, pady=8, undo=True)
        self.note.pack(fill="x")
        ttk.Label(export, text="내보내기 전 관련 Codex 작업을 멈추세요. 패키지는 암호화되지 않으며 대화에 민감한 내용이 포함될 수 있습니다.", wraplength=880).pack(anchor="w", pady=10)
        ttk.Button(export, text="이동 패키지 만들기", style="Accent.TButton", command=self.export).pack(anchor="e")
        self.path_row(restore, "이동 패키지", self.bundle, "file")
        self.path_row(restore, "새 복원 폴더", self.destination, "destination")
        ttk.Label(restore, text="새 폴더에 프로젝트와 독립된 Codex 저장소를 만듭니다. 원래 저장소는 유지됩니다.", wraplength=850).pack(anchor="w", pady=10)
        row = ttk.Frame(restore); row.pack(fill="x", pady=8)
        ttk.Button(row, text="패키지 확인", command=self.inspect).pack(side="left")
        ttk.Button(row, text="검증 후 복원", style="Accent.TButton", command=self.restore).pack(side="right")
        ttk.Separator(restore).pack(fill="x", pady=16)
        ttk.Button(restore, text="복원된 작업 열기", command=self.open_restored).pack(anchor="w")
        self.restore_label = tk.StringVar(value="복원 후 아래에서 대화를 선택해 이어갈 수 있습니다.")
        ttk.Label(restore, textvariable=self.restore_label, wraplength=850).pack(anchor="w", pady=10)
        self.resume_sessions = self.tree(restore, height=6)
        row = ttk.Frame(restore); row.pack(fill="x", pady=12)
        ttk.Button(row, text="1. 이 저장소에 로그인", command=lambda: self.launch(login=True)).pack(side="left")
        ttk.Button(row, text="2. 선택 대화 이어가기", style="Accent.TButton", command=self.launch).pack(side="right")
        ttk.Label(restore, text="Codex CLI가 필요합니다. 이어가기를 누르면 별도 터미널이 열립니다. 데스크톱 앱의 기존 채팅 목록과 자동 통합되지는 않습니다.", wraplength=850).pack(anchor="w")
        info = (
            "무엇이 어디에 저장되나요?\n\n"
            "프로젝트 폴더   소스 코드, 문서, AGENTS.md, 프로젝트별 .codex 설정\n"
            "Codex 저장소   기본 위치는 사용자 폴더의 .codex (CODEX_HOME으로 변경 가능)\n"
            "대화 기록   sessions / archived_sessions의 JSONL, state_*.sqlite 등의 인덱스\n"
            "작업 지침   AGENTS.md는 다시 읽는 지침입니다. 대화 기록이나 모델의 영구 기억과는 다릅니다.\n\n"
            "이 버전에서 옮기는 내용\n\n"
            "• 선택한 프로젝트 경로 및 하위 경로에 연결된 대화\n"
            "• 프로젝트 파일, 선택한 Git 기록, 대화 인덱스, 직접 작성한 인수인계 메모\n"
            "• 전역 AGENTS.md는 reference 폴더에 참고용으로 보존\n"
            "• 새 프로젝트 경로에 맞게 대화 메타데이터의 cwd를 변경\n\n"
            "지원 범위와 제한\n\n"
            "• 로그인 토큰, 전역 config.toml, 플러그인, 전역 메모리 DB는 옮기지 않습니다.\n"
            "• .env, 개인 키, 가상환경, node_modules, 링크는 기본 제외합니다.\n"
            "• 이름 기반 제외만 적용되므로 일반 코드·대화 속 비밀정보를 자동 제거하지는 않습니다.\n"
            "• 외부 이미지/첨부파일, 다른 경로의 worktree 및 하위 에이전트는 자동 수집하지 않습니다.\n"
            "• 과거 대화 본문에 적힌 옛 경로는 보존됩니다. 새 경로를 Codex에 알려주세요.\n"
            "• 저장 형식은 비공개 내부 구현에 의존합니다. 같은 Codex 버전에서 먼저 시험하세요.\n"
            "• SQLite 기록은 선택한 대화만 보존하며 복원 시 표시 캐시를 재생성하도록 초기화합니다.\n"
            "• 공식적인 PC 간 데스크톱 채팅 마이그레이션 도구는 아닙니다.\n\n"
            "패키지 가져오기는 신뢰하는 출처의 파일에만 사용하세요.\n"
            "프로젝트 설정·스크립트는 원본대로 포함됩니다. 자동 실행은 하지 않습니다."
        )
        text = tk.Text(about, wrap="word", font=("맑은 고딕", 11), relief="flat", bg="#eef2f6", padx=8, pady=8)
        text.insert("1.0", info); text.configure(state="disabled"); text.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="준비됨 · 모든 처리는 이 PC에서 수행됩니다.")
        ttk.Label(self, textvariable=self.status, padding=(24, 6)).pack(fill="x")
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.pack(fill="x", padx=24, pady=(0, 14))
        self.after(100, self.poll)

    def path_row(self, parent, label, variable, kind):
        row = ttk.Frame(parent); row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=8)
        def choose():
            if kind == "file":
                value = filedialog.askopenfilename(filetypes=[("이동 패키지", "*.codextransfer")])
            else:
                value = filedialog.askdirectory()
                if value and kind == "destination":
                    value = str(Path(value) / "TransferredProject")
            if value: variable.set(value)
        ttk.Button(row, text="찾아보기", command=choose).pack(side="right")

    def tree(self, parent, height):
        frame = ttk.Frame(parent); frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("title", "state"), show="headings", height=height, selectmode="browse")
        tree.heading("title", text="대화 제목"); tree.heading("state", text="상태")
        tree.column("title", width=650); tree.column("state", width=90, stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set); tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        return tree

    def fill(self, tree, sessions):
        tree.delete(*tree.get_children())
        for s in sessions:
            tree.insert("", "end", iid=s["id"], values=(s["title"], "보관됨" if s.get("archived") else "일반"))

    def run(self, job, done):
        if self.busy:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 후 실행하세요."); return
        self.busy = True; self.bar.start(12)
        def worker():
            try: self.events.put(("done", (done, job())))
            except Exception as exc: self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def report(self, text):
        self.events.put(("status", text))

    def poll(self):
        for _ in range(100):
            try: kind, data = self.events.get_nowait()
            except queue.Empty: break
            if kind == "status": self.status.set(data)
            else:
                self.busy = False; self.bar.stop()
                if kind == "error":
                    self.status.set("작업 실패 · 원본은 변경되지 않았습니다.")
                    messagebox.showerror("작업을 완료하지 못했습니다", data)
                else:
                    self.status.set("완료"); data[0](data[1])
        self.after(100, self.poll)

    def analyze(self):
        project, home, git = self.project.get(), self.home.get(), self.git.get()
        if not project or not home: return messagebox.showinfo("폴더 선택", "프로젝트와 저장소를 선택하세요.")
        def job():
            sessions, warnings = scan_sessions(home, project)
            files, excluded = project_files(project, git)
            return sessions, warnings, files, excluded
        def done(result):
            sessions, warnings, files, excluded = result
            self.fill(self.sessions, sessions)
            self.summary.set(f"대화 {len(sessions)}개 · 파일 {len(files)}개 · 제외 항목 {len(excluded)}개 · 경고 {len(warnings)}개")
            if warnings: messagebox.showwarning("확인 필요", "\n".join(warnings[:10]))
        self.run(job, done)

    def export(self):
        project, home = self.project.get(), self.home.get()
        if not project or not home: return messagebox.showinfo("폴더 선택", "프로젝트와 저장소를 선택하세요.")
        output = filedialog.asksaveasfilename(defaultextension=".codextransfer", initialfile=Path(project).name + ".codextransfer", filetypes=[("이동 패키지", "*.codextransfer")])
        if not output: return
        note, git = self.note.get("1.0", "end-1c"), self.git.get()
        self.run(lambda: export_bundle(project, home, output, note, git, self.report), lambda m: messagebox.showinfo("내보내기 완료", f"{output}\n\n대화 {len(m['sessions'])}개를 저장했습니다.\n다른 PC로 이 파일을 복사하세요."))

    def inspect(self):
        path = self.bundle.get()
        self.run(lambda: inspect_bundle(path), lambda m: messagebox.showinfo("패키지 정보", f"원본: {m['source_project']}\n생성: {m['created_at']}\n대화: {len(m['sessions'])}개\n파일: {len(m['files'])}개\n제외 항목: {len(m['excluded'])}개\n\n파일 내용의 SHA-256 검증은 복원 시 수행됩니다."))

    def restore(self):
        bundle, destination = self.bundle.get(), self.destination.get()
        if not bundle or not destination: return messagebox.showinfo("경로 선택", "패키지와 새 복원 폴더를 지정하세요.")
        self.run(lambda: restore_bundle(bundle, destination, self.report), lambda m: self.load_restored(Path(destination)))

    def load_restored(self, path):
        try:
            m = json.loads((path / "transfer.json").read_text(encoding="utf-8"))
            if m.get("format") != "codex-project-transfer" or not (path / "project").is_dir() or not (path / "codex").is_dir():
                raise ValueError("올바른 복원 폴더가 아닙니다.")
            if str(path.resolve()) != str(Path(m["restored_to"]).resolve()):
                raise ValueError("복원 폴더를 이동했습니다. 원본 패키지에서 새 경로로 다시 복원하세요.")
            self.restored = path
            self.fill(self.resume_sessions, m["sessions"])
            self.restore_label.set(str(path / "project"))
        except Exception as exc: messagebox.showerror("열기 실패", str(exc))

    def open_restored(self):
        value = filedialog.askdirectory()
        if value: self.load_restored(Path(value))

    def launch(self, login=False):
        if not self.restored: return messagebox.showinfo("복원 필요", "먼저 복원된 작업을 여세요.")
        exe = shutil.which("codex.exe") or shutil.which("codex")
        if not exe or Path(exe).suffix.lower() not in {".exe", ""}:
            exe = filedialog.askopenfilename(title="Codex CLI 실행 파일 선택", filetypes=[("Codex 실행 파일", "*.exe")])
        if not exe: return
        selection = self.resume_sessions.selection()
        if not login and not selection: return messagebox.showinfo("대화 선택", "이어갈 대화를 선택하세요.")
        env = os.environ.copy(); env["CODEX_HOME"] = str(self.restored / "codex")
        args = [exe, "login"] if login else [exe, "resume", selection[0], "-C", str(self.restored / "project")]
        try:
            subprocess.Popen(args, cwd=self.restored / "project", env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as exc: messagebox.showerror("실행 실패", str(exc))

    def close(self):
        if self.busy:
            messagebox.showinfo("처리 중", "파일 작업이 끝나면 닫아주세요."); return
        self.destroy()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        try:
            app = App(); app.withdraw(); app.update()
            result = {"ok": True, "tk": tk.TkVersion, "title": app.title()}
            app.destroy()
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
        sys.exit(0 if result["ok"] else 1)
    else:
        App().mainloop()
