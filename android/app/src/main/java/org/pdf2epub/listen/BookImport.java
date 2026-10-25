package org.pdf2epub.listen;

import android.content.*;
import android.net.Uri;
import android.os.*;
import android.provider.OpenableColumns;
import java.io.*;
import java.util.concurrent.*;

/** Application-owned import: rotation and screen navigation cannot lose progress. */
final class BookImport {
  static final BookImport INSTANCE = new BookImport();
  final Handler main = new Handler(Looper.getMainLooper());
  volatile boolean busy, cancelled;
  volatile long copied, total = -1, changed;
  volatile String filename = "Contacting file provider…", message = "";
  volatile File completed;
  volatile InputStream input;
  CancellationSignal signal;
  Runnable observer;

  void notifyUi() {
    main.post(
        () -> {
          if (observer != null) observer.run();
        });
  }

  synchronized void start(Context context, Uri uri) {
    if (busy) return;
    Context app = context.getApplicationContext();
    busy = true;
    cancelled = false;
    copied = 0;
    total = -1;
    completed = null;
    message = "";
    filename = "Contacting file provider…";
    changed = System.currentTimeMillis();
    signal = new CancellationSignal();
    notifyUi();
    new Thread(
            () -> {
              File partial = null;
              try {
                try (var cursor =
                    app.getContentResolver().query(uri, null, null, null, null, signal)) {
                  if (cursor != null && cursor.moveToFirst()) {
                    int n = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME),
                        size = cursor.getColumnIndex(OpenableColumns.SIZE);
                    filename = n >= 0 ? new File(cursor.getString(n)).getName() : "Audiobook.m4b";
                    if (size >= 0 && !cursor.isNull(size)) total = cursor.getLong(size);
                  }
                }
                if (!filename.matches("(?i).*\\.(m4b|m4a|mp3)$"))
                  throw new IOException("Choose an M4B, M4A or MP3 file.");
                notifyUi();
                partial = File.createTempFile("import-", ".partial", app.getFilesDir());
                try (var descriptor =
                    app.getContentResolver().openAssetFileDescriptor(uri, "r", signal)) {
                  if (descriptor == null)
                    throw new IOException("The file provider did not open this book.");
                  try (InputStream in = descriptor.createInputStream();
                      OutputStream out = new FileOutputStream(partial)) {
                    input = in;
                    byte[] buffer = new byte[65536];
                    int n;
                    long last = 0;
                    while ((n = in.read(buffer)) != -1) {
                      if (cancelled) throw new IOException("Cancelled");
                      out.write(buffer, 0, n);
                      copied += n;
                      changed = System.currentTimeMillis();
                      if (changed - last > 200) {
                        last = changed;
                        notifyUi();
                      }
                    }
                  }
                }
                if (cancelled) throw new IOException("Cancelled");
                if (copied == 0 || (total >= 0 && copied != total))
                  throw new IOException("Incomplete download. Please retry.");
                File target = new File(app.getFilesDir(), filename);
                if (target.exists())
                  target = new File(app.getFilesDir(), System.currentTimeMillis() + "-" + filename);
                if (!partial.renameTo(target))
                  throw new IOException("Cannot save the book. Check free storage.");
                completed = target;
                message = "Ready to listen · " + filename;
              } catch (Exception e) {
                if (partial != null) partial.delete();
                message = cancelled ? "Import cancelled" : "Import failed · " + e.getMessage();
              } finally {
                input = null;
                busy = false;
                notifyUi();
              }
            },
            "book-import")
        .start();
  }

  void cancel() {
    cancelled = true;
    message = "Cancelling…";
    notifyUi();
    new Thread(
            () -> {
              signal.cancel();
              try {
                InputStream in = input;
                if (in != null) in.close();
              } catch (IOException ignored) {
              }
            },
            "cancel-import")
        .start();
  }
}
