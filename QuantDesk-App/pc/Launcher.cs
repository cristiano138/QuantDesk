using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace QuantDesk.Watch
{
    /// <summary>
    /// QuantDesk 看盘台 —— 电脑端原生入口。
    /// 双击即打开看盘窗口：本机行情服务没起就把它带起来，已经开着就把窗口调到最前。
    /// 全程只连 127.0.0.1，不联网、不上云。
    /// 同一份源码编译成两个成品：QuantDesk.exe（打开软件）与 QuantDesk-安装程序.exe（装进开始菜单）。
    /// </summary>
    internal static class Program
    {
        private const string AppDisplayName = "QuantDesk 看盘台";
        private const string BackendUrl = "http://127.0.0.1:8787";
        private const string HealthPath = "/api/system/health";
        private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\QuantDesk";
        private const string WindowTitlePrefix = "QuantDesk";

        private static readonly string ExeDir =
            Path.GetDirectoryName(Application.ExecutablePath.TrimEnd('\\'));

        private static bool Silent;
        private static string ReportPath;

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string mode = null;
            foreach (string raw in args)
            {
                string a = raw.Trim();
                if (a.Length == 0) continue;
                string key = a.TrimStart('/', '-').ToLowerInvariant();
                if (key == "silent") { Silent = true; continue; }
                if (key.StartsWith("report=")) { ReportPath = a.Substring(a.IndexOf('=') + 1); continue; }
                if (a.Contains(":\\")) continue;                        // 报告文件路径，不是命令
                mode = key;
            }
            if (mode == null)
            {
#if SETUP
                mode = "install";
#else
                mode = "run";
#endif
            }

            if (mode == "selftest") return SelfTest(args);
            if (mode == "uninstall") return Uninstall();
            if (mode != "install" && mode != "run")
            {
                Notify("QuantDesk 看盘台",
                    "QuantDesk.exe            打开软件\n"
                    + "QuantDesk.exe /uninstall 从开始菜单与「应用」列表移除\n"
                    + "QuantDesk-安装程序.exe    装进开始菜单与桌面", false);
                return 2;
            }

            string root = FindRoot();
            if (root == null)
            {
                Notify(AppDisplayName,
                    "找不到软件本体。QuantDesk.exe 需要放在软件目录里（该目录要同时有 quantdesk 与 frontend\\dist）。\n\n" +
                    "当前所在目录：" + ExeDir, true);
                return 2;
            }

            return mode == "install" ? Install(root) : Run(root);
        }

        /// <summary>给用户看的话；/silent 时只写报告文件，不弹任何窗口（构建验收用）。</summary>
        private static void Notify(string title, string text, bool error)
        {
            if (Silent)
            {
                if (!string.IsNullOrEmpty(ReportPath))
                    File.AppendAllText(ReportPath, text + "\n", new UTF8Encoding(false));
                return;
            }
            MessageBox.Show(text, title, MessageBoxButtons.OK,
                error ? MessageBoxIcon.Error : MessageBoxIcon.Information);
        }

        // ---------------- 打开软件 ----------------

        private static int Run(string root)
        {
            Form splash = ShowSplash("正在准备行情…");

            bool up = Probe(800);
            if (!up)
            {
                SetSplash(splash, "本机行情服务没在运行，正在启动（第一次打开约 10~30 秒）…");
                if (!StartBackend(root))
                {
                    CloseSplash(splash);
                    Notify(AppDisplayName,
                        "启动失败：这台电脑上没找到能运行软件的 Python 环境。\n\n" +
                        "请确认已经安装 uv 或 Python 3.11+，并且软件目录里有 .venv。", true);
                    return 1;
                }
                up = WaitUntilReady(splash, 90);
            }
            CloseSplash(splash);

            if (!up)
            {
                Notify(AppDisplayName,
                    "行情服务没能就绪，软件打不开。\n\n"
                    + "可以稍等十几秒再双击一次；反复打不开时，把 "
                    + Path.Combine(root, "data", "logs") + " 这个目录发给开发者即可。", true);
                return 1;
            }

            IntPtr existing = FindAppWindow();
            if (existing != IntPtr.Zero)
            {
                ShowWindow(existing, 9);   // SW_RESTORE
                SetForegroundWindow(existing);
                return 0;
            }

            // 探到过不等于现在还在：起窗口前再确认一次，刚掉线就补一次启动，
            // 否则开出来的是「软件暂时打不开」的空壳窗口。
            if (!Probe(1200))
            {
                Form again = ShowSplash("行情服务掉线了，正在重新拉起…");
                bool revived = StartBackend(root) && WaitUntilReady(again, 90);
                CloseSplash(again);
                if (!revived)
                {
                    Notify(AppDisplayName,
                        "行情服务在启动过程中掉了一次。请再双击一次桌面图标。", true);
                    return 1;
                }
            }

            Process shell = StartShell(root);
            if (shell == null)
            {
                Notify(AppDisplayName, "看盘窗口启动失败，请重新双击图标再试。", true);
                return 1;
            }
            try
            {
                shell.WaitForExit();
            }
            catch (Exception)
            {
            }
            return 0;
        }

        private static bool WaitUntilReady(Form splash, int seconds)
        {
            DateTime deadline = DateTime.Now.AddSeconds(seconds);
            int tick = 0;
            while (DateTime.Now < deadline)
            {
                if (Probe(1200)) return true;
                tick++;
                if (splash != null && !splash.IsDisposed && tick % 4 == 0)
                {
                    SetSplash(splash, "正在准备行情… 已等待 "
                        + (int)(seconds - (deadline - DateTime.Now).TotalSeconds) + " 秒");
                }
                Thread.Sleep(500);
                if (splash != null && !splash.IsDisposed) Application.DoEvents();
            }
            return Probe(1200);
        }

        private static bool StartBackend(string root)
        {
            string file, extra;
            Runtime(root, out file, out extra);
            if (file == null) return TryUvBackend(root);
            return Spawn(root, file, extra + "-m quantdesk.app");
        }

        /// <summary>没有 .venv 时退一步用 uv 直接拉起（uv 会按 uv.lock 建好环境）。</summary>
        private static bool TryUvBackend(string root)
        {
            string uv = UvPath();
            if (uv == null) return false;
            return Spawn(root, uv, "run python -m quantdesk.app");
        }

        private static Process StartShell(string root)
        {
            string file, extra;
            Runtime(root, out file, out extra);
            if (file == null)
            {
                string uv = UvPath();
                if (uv == null) return null;
                return Start(uv, "run python desktop\\shell.py", root);
            }
            return Start(file, extra + "desktop\\shell.py", root);
        }

        private static bool Spawn(string root, string file, string arguments)
        {
            try
            {
                Process p = Start(file, arguments, root);
                return p != null;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static Process Start(string file, string arguments, string workDir)
        {
            ProcessStartInfo psi = new ProcessStartInfo(file, arguments);
            psi.WorkingDirectory = workDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            return Process.Start(psi);
        }

        /// <summary>
        /// 优先用软件目录自带的 Python，其次用系统里的 python.exe。
        /// 必须是 python.exe 而不是 pythonw.exe：uvicorn 建日志器时无条件读
        /// sys.stdout.isatty()，pythonw 的 sys.stdout 是 None，后端会在启动第一行就
        /// 抛 ValueError 直接退出。窗口不显示靠 CreateNoWindow，不靠换解释器。
        /// </summary>
        private static void Runtime(string root, out string file, out string prefix)
        {
            string venv = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (File.Exists(venv))
            {
                file = venv;
                prefix = "";
                return;
            }
            string py = OnPath("python.exe");
            if (py != null)
            {
                file = py;
                prefix = "";
                return;
            }
            file = null;
            prefix = null;
        }

        private static string UvPath()
        {
            string[] guesses = new string[]
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), @"\.local\bin\uv.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\uv\uv.exe"),
                OnPath("uv.exe")
            };
            foreach (string g in guesses)
                if (!string.IsNullOrEmpty(g) && File.Exists(g)) return g;
            return null;
        }

        private static string OnPath(string name)
        {
            string dirs = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string d in dirs.Split(';'))
            {
                if (d.Trim().Length == 0) continue;
                string p = Path.Combine(d.Trim().Trim('"'), name);
                try { if (File.Exists(p)) return p; }
                catch (ArgumentException) { }
            }
            return null;
        }

        private static bool Probe(int timeoutMs)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(BackendUrl + HealthPath);
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                req.Method = "GET";
                req.Proxy = null;
                using (HttpWebResponse res = (HttpWebResponse)req.GetResponse())
                {
                    return res.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        // ---------------- 安装 / 卸载 ----------------

        private static int Install(string root)
        {
            string launcher = Path.Combine(ExeDir, "QuantDesk.exe");
            if (!File.Exists(launcher))
            {
                Notify(AppDisplayName, "安装程序旁边缺少 QuantDesk.exe，两个文件要放在一起。", true);
                return 1;
            }

            List<string> made = new List<string>();
            List<string> failed = new List<string>();
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs\QuantDesk 看盘台.lnk");
            string desktop = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                "QuantDesk 看盘台.lnk");

            foreach (string target in new string[] { startMenu, desktop })
            {
                if (MakeShortcut(target, launcher, root)) made.Add(target);
                else failed.Add(target);
            }

            try
            {
                using (RegistryKey key = Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    key.SetValue("DisplayName", AppDisplayName);
                    key.SetValue("DisplayVersion", Version());
                    key.SetValue("Publisher", "QuantDesk");
                    key.SetValue("InstallLocation", root);
                    key.SetValue("DisplayIcon", launcher);
                    key.SetValue("UninstallString", "\"" + launcher + "\" /uninstall");
                    key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
                }
            }
            catch (Exception e)
            {
                failed.Add("写入「应用」列表失败：" + e.Message);
            }

            string text = "已装好：" + AppDisplayName + " v" + Version() + "\n\n";
            foreach (string m in made) text += "· " + m + "\n";
            text += "· 开始菜单与「设置 → 应用」里都能找到，卸载不会影响任何行情数据。\n";
            text += "\n软件位置：" + root;
            if (failed.Count > 0)
            {
                text += "\n\n没做成：";
                foreach (string f in failed) text += "\n· " + f;
            }
            Notify(AppDisplayName, text, failed.Count > 0);
            return failed.Count > 0 ? 1 : 0;
        }

        private static int Uninstall()
        {
            List<string> gone = new List<string>();
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs\QuantDesk 看盘台.lnk");
            string desktop = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                "QuantDesk 看盘台.lnk");
            foreach (string target in new string[] { startMenu, desktop })
            {
                try { if (File.Exists(target)) { File.Delete(target); gone.Add(target); } }
                catch (Exception) { }
            }
            try { Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false); }
            catch (Exception) { }

            Notify(AppDisplayName,
                "已从开始菜单和「设置 → 应用」里移除 " + AppDisplayName + "。\n\n"
                + "软件本体与行情数据还在：" + (FindRoot() ?? ExeDir)
                + "\n（要彻底删除，请自行清理这个文件夹。）", false);
            return 0;
        }

        /// <summary>快捷方式用 PowerShell 的 WScript.Shell 建，这是 Windows 自己带的组件。</summary>
        private static bool MakeShortcut(string lnkPath, string target, string workDir)
        {
            string dir = Path.GetDirectoryName(lnkPath);
            try { if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir)) Directory.CreateDirectory(dir); }
            catch (Exception) { }

            string script = new StringBuilder()
                .Append("$s = New-Object -ComObject WScript.Shell\n")
                .Append("$l = $s.CreateShortcut('").Append(Esc(lnkPath)).Append("')\n")
                .Append("$l.TargetPath = '").Append(Esc(target)).Append("'\n")
                .Append("$l.WorkingDirectory = '").Append(Esc(workDir)).Append("'\n")
                .Append("$l.IconLocation = '").Append(Esc(target)).Append(",0'\n")
                .Append("$l.Description = 'QuantDesk 看盘台（本地 A股 / 港股看盘）'\n")
                .Append("$l.Save()\n")
                .ToString();

            string tmp = Path.Combine(Path.GetTempPath(), "quantdesk-shortcut-" + Guid.NewGuid().ToString("N") + ".ps1");
            try
            {
                File.WriteAllText(tmp, script, new UTF8Encoding(true));
                ProcessStartInfo psi = new ProcessStartInfo(
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell\\v1.0\\powershell.exe"),
                    "-NoProfile -ExecutionPolicy Bypass -File \"" + tmp + "\"");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardError = true;
                Process p = Process.Start(psi);
                string err = p.StandardError.ReadToEnd();
                p.WaitForExit(15000);
                return p.HasExited && p.ExitCode == 0 && File.Exists(lnkPath);
            }
            catch (Exception)
            {
                return false;
            }
            finally
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch (Exception) { }
            }
        }

        private static string Esc(string s) { return s.Replace("'", "''"); }

        private static string Version()
        {
            try
            {
                string init = Path.Combine(FindRoot() ?? "", "quantdesk", "__init__.py");
                foreach (string line in File.ReadAllLines(init))
                {
                    if (line.TrimStart().StartsWith("__version__"))
                    {
                        int a = line.IndexOf('"'), b = line.LastIndexOf('"');
                        if (a >= 0 && b > a) return line.Substring(a + 1, b - a - 1);
                    }
                }
            }
            catch (Exception) { }
            return "1.1";
        }

        // ---------------- 找软件本体 ----------------

        /// <summary>从自己所在目录往上找，同时有 quantdesk 与 frontend\dist 的那层就是软件目录。</summary>
        private static string FindRoot()
        {
            DirectoryInfo dir = new DirectoryInfo(ExeDir);
            for (int i = 0; i < 6 && dir != null; i++)
            {
                if (Directory.Exists(Path.Combine(dir.FullName, "quantdesk"))
                    && File.Exists(Path.Combine(dir.FullName, "frontend", "dist", "index.html")))
                    return dir.FullName;
                dir = dir.Parent;
            }
            return null;
        }

        // ---------------- 窗口查找 ----------------

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        private static readonly EnumWindowsProc enumCb = CollectWindow;
        private static IntPtr found;

        [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int cmd);

        private static IntPtr FindAppWindow()
        {
            found = IntPtr.Zero;
            EnumWindows(enumCb, IntPtr.Zero);
            return found;
        }

        private static bool CollectWindow(IntPtr hWnd, IntPtr lParam)
        {
            if (!IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new StringBuilder(256);
            if (GetWindowText(hWnd, sb, sb.Capacity) <= 0) return true;
            if (sb.ToString().StartsWith(WindowTitlePrefix))
            {
                found = hWnd;
                return false;
            }
            return true;
        }

        // ---------------- 启动中的小窗口 ----------------

        private static Form ShowSplash(string text)
        {
            Form f = new Form();
            f.FormBorderStyle = FormBorderStyle.None;
            f.StartPosition = FormStartPosition.CenterScreen;
            f.Size = new Size(420, 96);
            f.BackColor = Color.FromArgb(16, 28, 46);
            f.TopMost = true;
            f.ShowInTaskbar = false;
            Label l = new Label();
            l.AutoSize = false;
            l.Bounds = new Rectangle(24, 22, 372, 52);
            l.ForeColor = Color.FromArgb(226, 233, 244);
            l.Font = new Font("Microsoft YaHei", 10.5f);
            l.Text = AppDisplayName + "\n" + text;
            f.Controls.Add(l);
            f.Tag = l;
            f.Show();
            f.Refresh();
            return f;
        }

        private static void SetSplash(Form f, string text)
        {
            Label l = f.Tag as Label;
            if (l != null) l.Text = AppDisplayName + "\n" + text;
            f.Refresh();
        }

        private static void CloseSplash(Form f)
        {
            try { if (f != null && !f.IsDisposed) f.Close(); } catch (Exception) { }
        }

        // ---------------- 自检（构建时验收用，不弹任何窗口） ----------------

        private static int SelfTest(string[] args)
        {
            string outPath = args.Length > 1 ? args[1] : Path.Combine(Path.GetTempPath(), "quantdesk-selftest.txt");
            File.WriteAllText(outPath, "", new UTF8Encoding(false));
            Step(outPath, "exe=" + Application.ExecutablePath);
            string root = FindRoot();
            Step(outPath, "root=" + (root ?? "(没找到)"));
            string file, prefix;
            Runtime(root ?? ExeDir, out file, out prefix);
            Step(outPath, "python=" + (file ?? "(无)"));
            Step(outPath, "uv=" + (UvPath() ?? "(无)"));
            Step(outPath, "health8787=" + (Probe(800) ? "200 OK" : "不可达"));
            Step(outPath, "appWindow=" + (FindAppWindow() == IntPtr.Zero ? "(未开)" : "已开"));
            Step(outPath, "version=" + Version());
            Step(outPath, "launcherNextToMe=" + File.Exists(Path.Combine(ExeDir, "QuantDesk.exe")));
            return 0;
        }

        private static void Step(string path, string line)
        {
            File.AppendAllText(path, line + "\n", new UTF8Encoding(false));
        }
    }
}
