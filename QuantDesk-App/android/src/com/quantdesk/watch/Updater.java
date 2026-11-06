package com.quantdesk.watch;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * 手机端的「一发布就能更新」：打开 App 时后台看一眼更新清单，有新版就问一句，
 * 下载完先核对发布时留下的 SHA-256，对得上才交给系统安装。
 *
 * 三条底线：
 * · 只走 HTTPS，且下载内容指纹与清单不一致就一个字节都不安装（更新通道被人换过一次，
 *   装进手机的就是别人的包）；
 * · 自动更新可以关（关了这个 App 一行网络都不为更新而发）；
 * · 装不装由系统弹窗决定，App 自己不做任何静默安装。
 *
 * 内部类一律具名：匿名内部类在 JDK 21 的 -target 8 下会生成描述符为空的
 * EnclosingMethod 属性，d8 解析时直接空指针。
 */
public final class Updater {

    /** 更新清单的位置：换渠道方只改这一处（或由设置写进 SharedPreferences 覆盖）。 */
    static final String DEFAULT_FEED =
            "https://github.com/cristiano138/QuantDesk/releases/latest/download/update.json";

    private static final String PREFS = "qd_update";
    static final String APK_NAME = "quantdesk-update.apk";   // UpdateFileProvider 只认这一个文件名
    private static final int CONNECT_MS = 12000;
    private static final int READ_MS = 30000;

    private Updater() { }

    /** 每次打开 App 都看一眼；关掉了就彻底不看。 */
    static void checkOnLaunch(final Activity act) {
        if (!autoOn(act)) return;
        start(act, false);
    }

    static void checkNow(Activity act) {
        start(act, true);
    }

    static boolean autoOn(Context ctx) {
        return prefs(ctx).getBoolean("auto_update", true);
    }

    static void setAutoUpdate(Context ctx, boolean on) {
        prefs(ctx).edit().putBoolean("auto_update", on).apply();
    }

    static String feedUrl(Context ctx) {
        String s = prefs(ctx).getString("update_feed", null);
        return s == null || s.trim().length() == 0 ? DEFAULT_FEED : s.trim();
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static void start(Activity act, boolean manual) {
        Thread t = new Thread(new CheckTask(act, manual));
        t.setDaemon(true);
        t.start();
    }

    // ---------------- 看一眼清单 ----------------

    private static final class CheckTask implements Runnable {
        private final Activity act;
        private final boolean manual;

        CheckTask(Activity act, boolean manual) {
            this.act = act;
            this.manual = manual;
        }

        @Override
        public void run() {
            final int mine = versionCode(act);
            String json;
            try {
                json = fetchText(feedUrl(act));
            } catch (Exception e) {
                if (manual) say(act, "连不上更新源，稍后再试一次");
                return;
            }
            final Info info = Info.parse(json, mine);
            if (info == null) {
                if (manual) say(act, "已经是最新版本（本机 " + mine + "）");
                return;
            }
            if (manual || !seen(act, info)) {
                // 同一版本问过一次的就不再骚扰（自动检查每次打开都会跑）；手动检查每次都问
                act.getWindow().getDecorView().post(new AskDialog(act, info));
            }
        }
    }

    /** 清单里的手机端部分：只有版本号比本机高、且 url 与指纹齐全，才算「有新版本」。 */
    private static final class Info {
        final String version;
        final String notes;
        final String url;
        final String sha256;
        final long size;

        Info(String version, String notes, String url, String sha256, long size) {
            this.version = version;
            this.notes = notes;
            this.url = url;
            this.sha256 = sha256;
            this.size = size;
        }

        static Info parse(String json, int mine) {
            if (json == null) return null;
            try {
                JSONObject top = new JSONObject(json);
                JSONObject a = top.optJSONObject("android");
                if (a == null) return null;
                int code = a.optInt("version_code", 0);
                String url = optText(a, "url");
                String sha = optText(a, "sha256");
                if (code <= mine) return null;
                if (url == null || !url.startsWith("https://")) return null;
                if (sha == null || !sha.matches("[0-9a-fA-F]{64}")) return null;
                return new Info(optText(top, "version"), optText(top, "notes"),
                        url, sha.toLowerCase(java.util.Locale.ROOT),
                        a.optLong("size", 0L));
            } catch (Exception e) {
                return null;            // 清单读不懂就当没有新版本，绝不拿半截信息去装东西
            }
        }

        private static String optText(JSONObject o, String key) {
            String s = o.optString(key, "");
            s = s == null ? null : s.trim();
            return s == null || s.length() == 0 ? null : s;
        }
    }

    // ---------------- 问一句：要不要更新 ----------------

    private static final class AskDialog implements Runnable {
        private final Activity act;
        private final Info info;

        AskDialog(Activity act, Info info) {
            this.act = act;
            this.info = info;
        }

        @Override
        public void run() {
            if (act.isFinishing()) return;
            remember(act, info.version);
            String size = info.size > 0 ? "（约 " + (info.size / 1024 / 1024 + 1) + " MB）" : "";
            String body = "新版本 " + info.version + size
                    + (info.notes == null ? "" : "\n\n这次更新：" + info.notes)
                    + "\n\n下载完会先核对发布时的校验码，对不上就不装。";
            new AlertDialog.Builder(act)
                    .setTitle("发现新版本")
                    .setMessage(body)
                    .setPositiveButton("现在更新", new InstallClick(act, info))
                    .setNegativeButton("以后再说", null)
                    .show();
        }
    }

    private static final class InstallClick implements DialogInterface.OnClickListener {
        private final Activity act;
        private final Info info;

        InstallClick(Activity act, Info info) {
            this.act = act;
            this.info = info;
        }

        @Override
        public void onClick(DialogInterface dialog, int which) {
            say(act, "正在下载新版本…");
            Thread t = new Thread(new DownloadTask(act, info));
            t.setDaemon(true);
            t.start();
        }
    }

    // ---------------- 下载 + 核对指纹 + 交给系统安装 ----------------

    private static final class DownloadTask implements Runnable {
        private final Activity act;
        private final Info info;

        DownloadTask(Activity act, Info info) {
            this.act = act;
            this.info = info;
        }

        @Override
        public void run() {
            File tmp = new File(act.getCacheDir(), APK_NAME);
            try {
                fetchFile(info.url, tmp);
            } catch (Exception e) {
                tmp.delete();
                say(act, "下载没成功：" + brief(e));
                return;
            }
            String got;
            try {
                got = sha256Of(tmp);
            } catch (Exception e) {
                tmp.delete();
                say(act, "这个包算不出校验码，已删除，不安装");
                return;
            }
            if (!got.equals(info.sha256)) {
                // 下载到的内容与发布时不一致：更新通道被人换过一次。删掉，一个字节都不装。
                tmp.delete();
                say(act, "校验码对不上，这个包不是发布时的那一个，已删除");
                return;
            }
            if (!install(act, tmp)) say(act, "这台手机没找到能安装应用程式的程序");
        }
    }

    private static boolean install(Context ctx, File apk) {
        try {
            Intent it = new Intent(Intent.ACTION_VIEW);
            it.setDataAndType(UpdateFileProvider.uriFor(ctx, apk),
                    "application/vnd.android.package-archive");
            it.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(it);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    // ---------------- 取数与工具 ----------------

    private static String fetchText(String url) throws Exception {
        HttpURLConnection c = open(url);
        try {
            InputStream in = c.getInputStream();
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            long total = 0;
            while ((n = in.read(buf)) > 0) {
                total += n;
                if (total > 1 << 20) throw new InterruptedIOException("清单超过 1 MB");
                out.write(buf, 0, n);
            }
            return new String(out.toByteArray(), "UTF-8");
        } finally {
            c.disconnect();
        }
    }

    private static void fetchFile(String url, File to) throws Exception {
        HttpURLConnection c = open(url);
        try {
            InputStream in = c.getInputStream();
            FileOutputStream out = new FileOutputStream(to);
            try {
                byte[] buf = new byte[1 << 15];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                out.flush();
            } finally {
                out.close();
            }
        } finally {
            c.disconnect();
        }
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(CONNECT_MS);
        c.setReadTimeout(READ_MS);
        c.setInstanceFollowRedirects(true);
        c.setRequestProperty("User-Agent", "QuantDesk-Watch");
        int code = c.getResponseCode();
        if (code < 200 || code >= 300) throw new IOException2("更新源返回 " + code);
        return c;
    }

    /** 只为了把「非 2xx」说成一句人话，不引额外依赖。 */
    private static final class IOException2 extends RuntimeException {
        IOException2(String msg) {
            super(msg);
        }
    }

    private static String sha256Of(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        InputStream in = new java.io.FileInputStream(f);
        try {
            byte[] buf = new byte[1 << 15];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        } finally {
            in.close();
        }
        byte[] d = md.digest();
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }

    private static int versionCode(Context ctx) {
        try {
            PackageInfo p = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return p.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    /** 同一个新版本只问一次：自动检查每次打开都跑，不能天天拦人。 */
    private static boolean seen(Context ctx, Info info) {
        return prefs(ctx).getString("asked", "").equals(info.version);
    }

    private static void remember(Context ctx, String version) {
        prefs(ctx).edit().putString("asked", version).apply();
    }

    private static String brief(Exception e) {
        String m = e == null ? null : e.getMessage();
        return m == null || m.length() == 0 ? (e == null ? "未知原因" : e.getClass().getSimpleName()) : m;
    }

    private static void say(final Activity act, final String text) {
        new Handler(Looper.getMainLooper()).post(new Say(act, text));
    }

    private static final class Say implements Runnable {
        private final Activity act;
        private final String text;

        Say(Activity act, String text) {
            this.act = act;
            this.text = text;
        }

        @Override
        public void run() {
            if (act.isFinishing()) return;
            Toast.makeText(act, text, Toast.LENGTH_LONG).show();
        }
    }
}
