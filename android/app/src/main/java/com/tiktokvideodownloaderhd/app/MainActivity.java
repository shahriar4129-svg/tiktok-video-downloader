package com.tiktokvideodownloaderhd.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.Toast;

public class MainActivity extends Activity {
    private static final String SITE_URL = "https://tiktokvideodownloaderhd.vercel.app/";
    private static final int WRITE_STORAGE_REQUEST = 42;

    private WebView webView;
    private ProgressBar progressBar;
    private String pendingUrl;
    private String pendingFileName;
    private String pendingMimeType;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        progressBar = findViewById(R.id.progress);
        progressBar.setMax(100);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setGeolocationEnabled(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleLink(request.getUrl());
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                progressBar.setVisibility(ProgressBar.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progressBar.setVisibility(ProgressBar.GONE);
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setProgress(newProgress);
                progressBar.setVisibility(
                        newProgress == 100 ? ProgressBar.GONE : ProgressBar.VISIBLE);
            }
        });

        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                Toast.makeText(this, "Open this file in your browser", Toast.LENGTH_SHORT).show();
                return;
            }
            String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
            queueDownload(url, fileName, mimeType);
        });

        if (savedInstanceState == null) {
            webView.loadUrl(SITE_URL);
        } else {
            webView.restoreState(savedInstanceState);
        }
    }

    private boolean isSiteHost(Uri uri) {
        String host = uri.getHost();
        if (host == null) return false;
        return host.equalsIgnoreCase("tiktokvideodownloaderhd.vercel.app")
                || host.equalsIgnoreCase("www.tiktokvideodownloaderhd.vercel.app");
    }

    private boolean handleLink(Uri uri) {
        String scheme = uri.getScheme();
        if (scheme == null) return false;
        if (scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https")) {
            if (isSiteHost(uri)) return false;
        } else if (scheme.equalsIgnoreCase("about") || scheme.equalsIgnoreCase("data")) {
            return false;
        }

        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException error) {
            Toast.makeText(this, "No app can open this link", Toast.LENGTH_SHORT).show();
        }
        return true;
    }

    private void queueDownload(String url, String fileName, String mimeType) {
        if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            pendingUrl = url;
            pendingFileName = fileName;
            pendingMimeType = mimeType;
            requestPermissions(
                    new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, WRITE_STORAGE_REQUEST);
            return;
        }
        enqueueDownload(url, fileName, mimeType);
    }

    private void enqueueDownload(String url, String fileName, String mimeType) {
        DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
        request.setTitle(fileName);
        request.setDescription("Saved by TikTok Video Downloader HD");
        if (mimeType != null && !mimeType.isEmpty()) request.setMimeType(mimeType);
        request.setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
        request.allowScanningByMediaScanner();

        DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        if (manager == null) {
            Toast.makeText(this, "Download service unavailable", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            manager.enqueue(request);
            Toast.makeText(this, "Download started", Toast.LENGTH_SHORT).show();
        } catch (Exception error) {
            Toast.makeText(this, "Could not start download", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == WRITE_STORAGE_REQUEST
                && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED
                && pendingUrl != null) {
            enqueueDownload(pendingUrl, pendingFileName, pendingMimeType);
        }
        pendingUrl = null;
        pendingFileName = null;
        pendingMimeType = null;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (webView != null) webView.saveState(outState);
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.setWebViewClient(null);
            webView.setWebChromeClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
