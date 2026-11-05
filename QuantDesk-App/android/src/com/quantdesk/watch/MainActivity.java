package com.quantdesk.watch;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ProgressBar;

/**
 * 看盘 App 的原生外壳：一个全屏 WebView 装载随包自带的看盘页面。
 * 页面与数据都在这台手机上，不经过电脑，也没有自建服务器。
 *
 * 内部类一律具名：匿名内部类在 JDK 21 的 -target 8 下会生成描述符为空的
 * EnclosingMethod 属性，d8 解析时直接空指针。
 */
public class MainActivity extends Activity {

    static final String START_URL = "file:///android_asset/index.html";
    static final String OFFLINE_PAGE =
            "<!doctype html><html><head><meta charset='utf-8'>"
            + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<style>body{margin:0;padding:44px 24px;background:#eef1f6;color:#16202f;"
            + "font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}"
            + "h3{margin:0 0 10px;font-size:17px}p{margin:0;color:#5b6a7d;font-size:14px;line-height:1.7}"
            + "</style></head><body>"
            + "<h3>暂时取不到行情</h3>"
            + "<p>这台手机现在没有可用的移动网络或 Wi-Fi。行情来自公开的股市数据接口，需要联网才能取到。</p>"
            + "<p>网络恢复后会自动重新加载。</p>"
            + "<script>setTimeout(function(){location.replace('" + START_URL + "');},6000);</script>"
            + "</body></html>";

    private static final int CHROME_DARK = 0xFF101C2E;

    private WebView web;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().setStatusBarColor(CHROME_DARK);
        getWindow().setNavigationBarColor(CHROME_DARK);

        web = new WebView(this);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setVisibility(View.GONE);

        FrameLayout root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        FrameLayout.LayoutParams barLp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(2));
        barLp.gravity = Gravity.TOP;
        root.addView(progress, barLp);
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setTextZoom(100);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setLoadsImagesAutomatically(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);

        web.setBackgroundColor(CHROME_DARK);
        web.setWebViewClient(new PageGuard());
        web.setWebChromeClient(new LoadBar(progress));

        web.loadUrl(START_URL);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    /** 取不到行情时给一句人话，而不是浏览器错误页。只处理整页失败，不误伤单个数据请求。 */
    private static final class PageGuard extends WebViewClient {
        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request != null && request.isForMainFrame()) {
                view.loadDataWithBaseURL(START_URL, OFFLINE_PAGE, "text/html", "utf-8", null);
            }
        }
    }

    private static final class LoadBar extends WebChromeClient {
        private final ProgressBar bar;

        LoadBar(ProgressBar bar) {
            this.bar = bar;
        }

        @Override
        public void onProgressChanged(WebView view, int newProgress) {
            bar.setProgress(newProgress);
            bar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
        }
    }
}
