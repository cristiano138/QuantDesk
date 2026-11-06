using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace QuantDesk.Watch
{
    /// <summary>
    /// QuantDesk 看盘台 —— 电脑端原生入口。
    /// 双击即打开看盘窗口：本机行情服务没起就把它带起来，已经开着就把窗口调到最前。
    /// 行情与下单全程只连 127.0.0.1。唯一的出网口子是「自动更新」：开机后到发布渠道
    /// 取一份 update.json，比对版本号，下载更新包并核 SHA-256，校验通过才动现有文件；
    /// 不想出网就 `QuantDesk.exe -auto-update=off`（关掉后一行网络都不发）。
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

        /// <summary>弹窗里的换行统一用它，源码里就不必到处写转义符。</summary>
        private static readonly string NL = Environment.NewLine;

        private static bool Silent;
        private static string ReportPath;
        private static Process ShellProc;      // 看盘窗口进程是我们自己拉起来的，更新时只关它

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string mode = null;
            List<string> paths = new List<string>();
            foreach (string raw in args)
            {
                string a = raw.Trim();
                if (a.Length == 0) continue;
                string key = a.TrimStart('/', '-').ToLowerInvariant();
                if (key == "silent") { Silent = true; continue; }
                if (key.StartsWith("report=")) { ReportPath = a.Substring(a.IndexOf('=') + 1); continue; }
                if (a.Contains(":\\")) { paths.Add(a); continue; }    // 带盘符的是路径参数，不是命令
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

            // GitHub 那边只收 TLS 1.2 起的握手，.NET 4.x 默认协议谈不下来
            try { ServicePointManager.SecurityProtocol = (SecurityProtocolType)(0xC0 | 0x300 | 0xC00); }
            catch (Exception) { }

            if (mode == "selftest") return SelfTest(args);
            if (mode == "apply") return Apply(paths);        // 换文件的帮手：路径里可能带中文，不做 FindRoot
            if (mode == "check-update")
            {
                // root 要到下面才算过一遍校验；手动检查更新只需要「软件目录」，自己取
                CheckUpdateThread(new object[] { FindRoot() ?? ExeDir, true });
                return 0;
            }
            if (mode == "auto-update=off")
            {
                SetAutoUpdate(false);
                Notify(AppDisplayName,
                    "已关闭自动更新：以后打开软件不会自己去找新版本。" + NL
                    + "随时可以重新打开：QuantDesk.exe -auto-update=on" + NL
                    + "更新源：" + FeedUrl(), false);
                return 0;
            }
            if (mode == "auto-update=on")
            {
                SetAutoUpdate(true);
                Notify(AppDisplayName,
                    "已开启自动更新：每次打开软件会去更新源看一眼有没有新版本。" + NL
                    + "更新源：" + FeedUrl(), false);
                return 0;
            }
            if (mode == "uninstall") return Uninstall();
            if (mode != "install" && mode != "run")
            {
                Notify("QuantDesk 看盘台",
                    "QuantDesk.exe              打开软件（自动检查新版本）\n"
                    + "QuantDesk.exe /check-update 立刻检查一次更新\n"
                    + "QuantDesk.exe /auto-update=off  关闭自动更新（此后一行网络都不发）\n"
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
            // 上一轮后台下好并验过的更新，在这里换上——用户不需要点任何东西。
            // 放在弹启动画面之前：换文件要动的就是正在被这个进程占用的东西，越早越省事。
            ApplyPending(root);

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
            ShellProc = shell;
            // 窗口已经开出来了，检查更新放到后台线程：不占启动时间，也不弹窗拦人。
            MaybeUpdateInBackground(root);
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

// ---------------- 自动更新（一发布，客户端就能收到） ----------------

        private const string DefaultFeed =
            "https://github.com/cristiano138/QuantDesk/releases/latest/download/update.json";
        private const string SettingsKey = @"Software\QuantDesk";
        private const string DoubleQuote = "\"";     // 拼命令行时用它，源码里少几层转义
        // 软件本体在软件目录里的相对位置。更新包必须按同一棵树打包，铺下去才对得上。
        private const string ExeRelPath = @"QuantDesk-App\QuantDesk.exe";

        /// <summary>更新源整条可以换掉：GitHub 在国内直连不稳时，渠道方把同一份 update.json 放任何 HTTPS 位置。</summary>
        private static string FeedUrl()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(SettingsKey))
                {
                    object v = k == null ? null : k.GetValue("update_feed");
                    string s = v == null ? null : Convert.ToString(v);
                    if (!string.IsNullOrEmpty(s)) return s.Trim();
                }
            }
            catch (Exception) { }
            return DefaultFeed;
        }

        private static bool AutoUpdateOn()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(SettingsKey))
                {
                    if (k == null) return true;
                    object v = k.GetValue("auto_update");
                    if (v == null) return true;                 // 没写过就是默认开
                    return Convert.ToString(v) != "0";
                }
            }
            catch (Exception) { }
            return true;
        }

        private static void SetAutoUpdate(bool on)
        {
            RegistryKey root = Registry.CurrentUser.CreateSubKey(SettingsKey);
            if (root != null) root.SetValue("auto_update", on ? 1 : 0, RegistryValueKind.DWord);
        }

        private static string UpdateTemp
        {
            get { return Path.Combine(Path.GetTempPath(), "QuantDeskUpdate"); }
        }

        private static string PendingDir
        {
            get { return Path.Combine(UpdateTemp, "pending"); }
        }

        private static string PendingMarker
        {
            get { return Path.Combine(UpdateTemp, "pending.txt"); }
        }

        private static void MaybeUpdateInBackground(string root)
        {
            if (!AutoUpdateOn()) return;
            Thread t = new Thread(CheckUpdateThread);
            t.IsBackground = true;
            t.Start(new object[] { root, false });
        }

        private static void CheckUpdateThread(object arg)
        {
            object[] a = (object[])arg;
            bool manual = (bool)a[1];
            try
            {
                CheckUpdate((string)a[0], manual);
            }
            catch (Exception e)
            {
                // 后台自动检查失败不打扰人（下次启动还会再问）；手动检查必须原样报出来
                if (manual) Notify(AppDisplayName, "检查更新没成功：" + e.Message, true);
            }
        }

        private static void CheckUpdate(string root, bool manual)
        {
            string feed = FeedUrl();
            string json = DownloadText(feed);
            if (string.IsNullOrEmpty(json))
            {
                if (manual) Notify(AppDisplayName,
                    "没能连上更新源，拿不到最新版本信息。" + NL + NL + "更新源：" + feed, true);
                return;
            }
            string pc = JsonBlock(json, "pc");
            if (pc == null)
            {
                if (manual) Notify(AppDisplayName,
                    "更新清单里没有电脑端的部分（清单格式不对）。" + NL + "更新源：" + feed, true);
                return;
            }
            string newest = JsonStr(json, "version");   // 版本号在清单顶层：一次发布对应一个版本
            string url = JsonStr(pc, "url");
            string sha = JsonStr(pc, "sha256");
            string notes = JsonStr(json, "notes");
            long size = JsonNum(pc, "size");
            string mine = Version();
            if (string.IsNullOrEmpty(newest) || string.IsNullOrEmpty(url))
            {
                if (manual) Notify(AppDisplayName, "更新清单缺字段（version 或 url），不动现有文件。", true);
                return;
            }
            if (CompareVersion(mine, newest) >= 0)
            {
                if (manual) Notify(AppDisplayName,
                    "已经是最新版本（本机 " + mine + "，更新源 " + newest + "）。" + NL + "更新源：" + feed, false);
                return;
            }

            string dir = Path.Combine(PendingDir, newest);
            string files = Path.Combine(dir, "files");
            string zip = Path.Combine(dir, "QuantDesk-PC.zip");
            bool ready = File.Exists(PendingMarker)
                && string.Equals(ReadMarkerVersion(), newest, StringComparison.Ordinal)
                && File.Exists(Path.Combine(files, ExeRelPath));
            if (!ready)
            {
                // 上一轮解压到一半留下的残渣会让 ExtractToDirectory 直接抛「文件已存在」，
                // 那样这个版本就永远备不好、每次打开都静默失败——先清干净这一版的暂存目录。
                try { if (Directory.Exists(dir)) Directory.Delete(dir, true); } catch (Exception) { }
                Directory.CreateDirectory(files);
                TryDelete(zip);
                DownloadFile(url, zip);
                // SHA-256 对得上才允许往软件目录里写任何东西：更新通道被人换过一次，
                // 后果就是把别人的文件装进用户机器。校验不过就一个字节都不动。
                string got = Sha256File(zip);
                if (!string.IsNullOrEmpty(sha) && sha.Length == 64
                    && !string.Equals(got, sha, StringComparison.OrdinalIgnoreCase))
                {
                    TryDelete(zip);
                    Notify(AppDisplayName,
                        "更新包校验没过（下载到的内容与发布时不一致），已放弃本次更新，现有文件没动。"
                        + NL + NL + "期望 " + sha + NL + "实际 " + got, true);
                    return;
                }
                if (size > 0 && new FileInfo(zip).Length != size)
                {
                    TryDelete(zip);
                    Notify(AppDisplayName, "更新包大小与清单不符，已放弃本次更新。", true);
                    return;
                }
                System.IO.Compression.ZipFile.ExtractToDirectory(zip, files);
                TryDelete(zip);                                  // 解出来的包不再占临时目录
                if (!File.Exists(Path.Combine(files, ExeRelPath)))
                {
                    Notify(AppDisplayName, "更新包里没有 QuantDesk.exe，不动现有文件。", true);
                    return;
                }
                WriteMarker(newest, got);
            }

            if (!manual)
            {
                // 后台路径不打扰人：这一轮先备好，下次打开软件在启动画面之前自动换上。
                try
                {
                    File.AppendAllText(Path.Combine(UpdateTemp, "history.txt"),
                        DateTime.Now.ToString("s") + "  备好 " + newest + NL, new UTF8Encoding(false));
                }
                catch (Exception) { }
                return;
            }
            string what = "新版本 " + newest + "（本机 " + mine + "）。"
                + (string.IsNullOrEmpty(notes) ? "" : NL + NL + "这次更新：" + notes);
            if (AskYesNo(what + NL + NL + "现在就更新（会关掉软件重开一次）吗？"
                         + NL + "选「否」则下次打开软件时自动完成更新。"))
                ApplyNow(root, files);
        }

        private static string ReadMarkerVersion()
        {
            try
            {
                string[] lines = File.ReadAllLines(PendingMarker);
                return lines.Length > 0 ? lines[0].Trim() : null;
            }
            catch (Exception) { return null; }
        }

        private static void WriteMarker(string version, string sha)
        {
            File.WriteAllText(PendingMarker, version + NL + sha + NL, new UTF8Encoding(false));
        }

        /// <summary>启动时先看有没有备好的更新：有就直接换上，用户不需要点任何东西。</summary>
        private static bool ApplyPending(string root)
        {
            string newest = ReadMarkerVersion();
            if (string.IsNullOrEmpty(newest)) return false;
            if (string.Equals(newest, Version(), StringComparison.Ordinal)) return false;
            string files = Path.Combine(PendingDir, newest, "files");
            if (!File.Exists(Path.Combine(files, ExeRelPath))) return false;
            ApplyNow(root, files);
            return true;
        }

        private static bool AskYesNo(string text)
        {
            if (Silent) return false;
            return MessageBox.Show(text, AppDisplayName, MessageBoxButtons.YesNo,
                                   MessageBoxIcon.Question) == DialogResult.Yes;
        }

        /// <summary>
        /// 换文件：把本体复制一份到临时目录，由那一份「帮手」去铺新文件。
        /// 为什么非要换一个进程：正在运行的 exe 不许被覆盖（改名可以、覆盖不行），
        /// 铺新文件这一步不能由「正被铺的那个文件」自己来完成。
        /// 不用 cmd + bat：cmd 按 OEM 代码页解析命令行，软件目录带中文时整条命令直接不执行，
        /// 还不留报错（本机实测 code=1、脚本一行都没跑）。C# 走宽字符 API 没有这个坑。
        /// </summary>
        private static void ApplyNow(string root, string files)
        {
            if (ShellProc != null)
            {
                // 只关我们自己拉起来的看盘窗口进程，不碰这台机器上任何别的程序。
                try { ShellProc.Kill(); } catch (Exception) { }
            }
            Directory.CreateDirectory(UpdateTemp);
            string helper = Path.Combine(UpdateTemp, "QuantDesk-apply.exe");
            try
            {
                TryDelete(helper);                                  // 上一轮的帮手早已退场
                File.Copy(Application.ExecutablePath, helper, true);
            }
            catch (Exception e)
            {
                UpdateLog("帮手没能准备：" + e.Message + "（更新包保留，下次打开软件再试）");
                return;
            }
            string args = (Silent ? "/silent " : "") + "/apply "
                + DoubleQuote + root.TrimEnd('\\') + DoubleQuote + " "
                + DoubleQuote + files.TrimEnd('\\') + DoubleQuote + " "
                + DoubleQuote + PendingMarker + DoubleQuote;
            UpdateLog("帮手启动：" + args);
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(helper, args);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WorkingDirectory = UpdateTemp;
                Process child = Process.Start(psi);
                UpdateLog("帮手已起 pid=" + (child == null ? "(null)" : child.Id.ToString()));
            }
            catch (Exception e)
            {
                UpdateLog("帮手没能启动：" + e.Message + "（更新包保留，下次打开软件再试）");
                return;
            }
            Environment.Exit(0);                    // 立刻退场，把 exe 的占用交还给帮手
        }

        /// <summary>帮手进程的执行体：等旧进程松手 → 逐文件覆盖 → 删标记 → 重开软件。</summary>
        private static int Apply(List<string> paths)
        {
            if (paths.Count < 3)
            {
                UpdateLog("换文件参数不全（要 软件目录 / 新文件目录 / 标记文件），什么都没动");
                return 2;
            }
            string root = paths[0].TrimEnd('\\'), files = paths[1].TrimEnd('\\'), marker = paths[2];
            DateTime deadline = DateTime.Now.AddSeconds(40);
            int copied = -1;
            string lastError = "(一次都没试成)";
            while (DateTime.Now < deadline)
            {
                try
                {
                    copied = CopyTree(files, root);
                    break;
                }
                catch (Exception e)
                {
                    lastError = e.Message;              // 多半是旧进程还没退，本体还锁着
                    Thread.Sleep(500);
                }
            }
            if (copied < 0)
            {
                // 铺不动就把标记留着：下次打开软件再试一次，绝不假装已经更新过
                UpdateLog("换文件没成：" + lastError + "（更新包保留，下次打开软件再试）");
                return 1;
            }
            TryDelete(marker);
            UpdateLog("已铺 " + copied + " 个文件，重开软件");
            if (Silent)
            {
                // 构建验收走的就是这条路：文件已经换上就够了，不要在验收机上开窗口
                UpdateLog("（验收模式：不自动重开软件）");
                return 0;
            }
            try
            {
                ProcessStartInfo go = new ProcessStartInfo(Path.Combine(root, ExeRelPath));
                go.WorkingDirectory = root;
                Process.Start(go);
            }
            catch (Exception e)
            {
                UpdateLog("软件没能自己重开，请再双击一次桌面图标：" + e.Message);
            }
            return 0;
        }

        /// <summary>只叠加不镜像：包里有什么就铺什么，目标目录里多余的文件（用户自己的策略与数据）一概不动。</summary>
        private static int CopyTree(string src, string dst)
        {
            int n = 0;
            Directory.CreateDirectory(dst);
            foreach (string f in Directory.GetFiles(src))
            {
                File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
                n++;
            }
            foreach (string d in Directory.GetDirectories(src))
                n += CopyTree(d, Path.Combine(dst, Path.GetFileName(d)));
            return n;
        }

        private static string DownloadText(string url)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.UserAgent = "QuantDesk-UpdateCheck";
                req.Timeout = 12000;
                req.ReadWriteTimeout = 12000;
                using (WebResponse r = req.GetResponse())
                using (Stream s = r.GetResponseStream())
                using (StreamReader sr = new StreamReader(s, Encoding.UTF8))
                    return sr.ReadToEnd();
            }
            catch (Exception)
            {
                return null;                 // 拿不到就说拿不到，绝不用旧清单冒充新版本
            }
        }

        private static void DownloadFile(string url, string to)
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.UserAgent = "QuantDesk-UpdateCheck";
            req.Timeout = 20000;
            req.ReadWriteTimeout = 60000;
            using (WebResponse r = req.GetResponse())
            using (Stream s = r.GetResponseStream())
            using (FileStream f = File.Create(to))
            {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = s.Read(buf, 0, buf.Length)) > 0) f.Write(buf, 0, n);
            }
        }

        private static void TryDelete(string path)
        {
            try { if (File.Exists(path)) File.Delete(path); } catch (Exception) { }
        }

        /// <summary>更新过程的流水（%TEMP%\QuantDeskUpdate\update.log）——出问题时先看这里。</summary>
        private static void UpdateLog(string line)
        {
            try
            {
                Directory.CreateDirectory(UpdateTemp);
                File.AppendAllText(Path.Combine(UpdateTemp, "update.log"),
                                   DateTime.Now.ToString("s") + "  " + line + NL, new UTF8Encoding(false));
            }
            catch (Exception) { }
        }

        private static string Sha256File(string path)
        {
            using (FileStream s = File.OpenRead(path))
            using (SHA256 h = SHA256.Create())
            {
                byte[] hash = h.ComputeHash(s);
                StringBuilder sb = new StringBuilder(hash.Length * 2);
                for (int i = 0; i < hash.Length; i++) sb.Append(hash[i].ToString("x2"));
                return sb.ToString();
            }
        }

        // ---------------- 从清单里取字段（不引第三方 JSON 库） ----------------

        private static string JsonBlock(string json, string key)
        {
            int i = json.IndexOf(DoubleQuote + key + DoubleQuote, StringComparison.Ordinal);
            if (i < 0) return null;
            int b = json.IndexOf('{', i);
            if (b < 0) return null;
            int depth = 0;
            for (int p = b; p < json.Length; p++)
            {
                if (json[p] == '{') depth++;
                else if (json[p] == '}')
                {
                    depth--;
                    if (depth == 0) return json.Substring(b, p - b + 1);
                }
            }
            return null;
        }

        private static string JsonStr(string json, string key)
        {
            int i = json.IndexOf(DoubleQuote + key + DoubleQuote, StringComparison.Ordinal);
            if (i < 0) return null;
            int c = json.IndexOf(':', i);
            if (c < 0) return null;
            int q1 = json.IndexOf('"', c + 1);
            if (q1 < 0) return null;
            int q2 = q1 + 1;
            while (q2 < json.Length)
            {
                if (json[q2] == '"' && json[q2 - 1] != '\\') break;
                q2++;
            }
            if (q2 >= json.Length) return null;
            return json.Substring(q1 + 1, q2 - q1 - 1);
        }

        private static long JsonNum(string json, string key)
        {
            int i = json.IndexOf(DoubleQuote + key + DoubleQuote, StringComparison.Ordinal);
            if (i < 0) return 0;
            int c = json.IndexOf(':', i);
            if (c < 0) return 0;
            StringBuilder sb = new StringBuilder();
            for (int p = c + 1; p < json.Length; p++)
            {
                char ch = json[p];
                if (ch >= '0' && ch <= '9') sb.Append(ch);
                else if (sb.Length > 0) break;
            }
            long v;
            return long.TryParse(sb.ToString(), out v) ? v : 0;
        }

        private static int CompareVersion(string a, string b)
        {
            string[] pa = (a ?? "").Split('.'), pb = (b ?? "").Split('.');
            int n = Math.Max(pa.Length, pb.Length);
            for (int i = 0; i < n; i++)
            {
                int x = PartNum(pa, i), y = PartNum(pb, i);
                if (x != y) return x < y ? -1 : 1;
            }
            return 0;
        }

        private static int PartNum(string[] parts, int i)
        {
            if (i >= parts.Length) return 0;
            StringBuilder sb = new StringBuilder();
            foreach (char ch in parts[i])
            {
                if (ch < '0' || ch > '9') break;          // "2-beta" 取 2
                sb.Append(ch);
            }
            int v;
            return int.TryParse(sb.ToString(), out v) ? v : 0;
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
            Step(outPath, "update_feed=" + FeedUrl());
            Step(outPath, "auto_update=" + (AutoUpdateOn() ? "on" : "off"));
            Step(outPath, "pending=" + (ReadMarkerVersion() ?? "(无)"));
            Step(outPath, "launcherNextToMe=" + File.Exists(Path.Combine(ExeDir, "QuantDesk.exe")));
            return 0;
        }

        private static void Step(string path, string line)
        {
            File.AppendAllText(path, line + "\n", new UTF8Encoding(false));
        }
    }
}
