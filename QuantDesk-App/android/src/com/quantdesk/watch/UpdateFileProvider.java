package com.quantdesk.watch;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 只把「刚下载好、且校验码核对过的那一个安装包」以 content:// 交出去。
 *
 * 为什么不直接给 file://：Android 7 起，跨应用递一条 file:// 路径会被系统当成
 * FileUriExposedException 直接掐掉——更新会在装的时候才失败，前面一路都是绿的。
 * 这里只放行 cache 目录里那个固定名字的文件，别的一概不见。
 */
public final class UpdateFileProvider extends ContentProvider {

    private static final String AUTHORITY = "com.quantdesk.watch.file";

    static Uri uriFor(android.content.Context ctx, File apk) {
        return Uri.parse("content://" + AUTHORITY + "/" + apk.getName());
    }

    /** 认路径：只认 cache 里那个唯一的安装包文件名，其他一律当作没有。 */
    private File resolve(Uri uri) {
        if (uri == null) return null;
        String name = uri.getLastPathSegment();
        if (!Updater.APK_NAME.equals(name)) return null;
        File f = new File(getContext().getCacheDir(), name);
        return f.isFile() ? f : null;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = resolve(uri);
        if (f == null) throw new FileNotFoundException("只递出刚下载好的安装包");
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        File f = resolve(uri);
        if (f == null) return null;
        MatrixCursor c = new MatrixCursor(
                new String[]{MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.SIZE});
        c.addRow(new Object[]{f.getName(), f.length()});
        return c;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException();
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException();
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException();
    }
}
