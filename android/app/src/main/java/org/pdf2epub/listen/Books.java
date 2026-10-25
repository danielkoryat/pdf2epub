package org.pdf2epub.listen;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.MediaMetadataRetriever;
import android.net.Uri;
import android.util.LruCache;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import java.io.File;
import java.util.*;

/**
 * The books saved in the app's storage and what the app remembers about each: where the listener
 * stopped, the speed, the length, title and author. Shared by the screens and the playback service
 * (one process), keyed by file name.
 */
final class Books {
  static final String AUDIO = "(?i).*\\.(m4b|m4a|mp3)$";
  static final LruCache<String, Bitmap> covers =
      new LruCache<>((int) Math.min(Runtime.getRuntime().maxMemory() / 8, 24 << 20)) {
        protected int sizeOf(String key, Bitmap value) {
          return value.getAllocationByteCount();
        }
      };
  static final Set<String> noCover = Collections.synchronizedSet(new HashSet<>());

  static SharedPreferences prefs(Context c) {
    SharedPreferences p = c.getSharedPreferences("books", Context.MODE_PRIVATE);
    if (!p.getBoolean("migrated", false)) {
      // Version 0.3 kept positions and speeds in MainActivity's own preferences.
      SharedPreferences.Editor e = p.edit();
      for (var entry : c.getSharedPreferences("MainActivity", 0).getAll().entrySet()) {
        Object v = entry.getValue();
        if (v instanceof Long l) e.putLong(entry.getKey(), l);
        else if (v instanceof Float f) e.putFloat(entry.getKey(), f);
      }
      e.putBoolean("migrated", true).commit();
    }
    return p;
  }

  /** Books on this device, the most recently played or added first. */
  static List<File> list(Context c) {
    File[] files = c.getFilesDir().listFiles(f -> f.isFile() && f.getName().matches(AUDIO));
    if (files == null) return new ArrayList<>();
    SharedPreferences p = prefs(c);
    List<File> books = new ArrayList<>(Arrays.asList(files));
    books.sort(
        Comparator.comparingLong(
                (File f) -> Math.max(p.getLong(f.getName() + ".played", 0), f.lastModified()))
            .reversed());
    return books;
  }

  static long position(Context c, File f) {
    return prefs(c).getLong(f.getName(), 0);
  }

  static float speed(Context c, File f) {
    return prefs(c).getFloat(f.getName() + ".speed", 1);
  }

  /** The length in milliseconds, or 0 when not known yet. */
  static long duration(Context c, File f) {
    return prefs(c).getLong(f.getName() + ".duration", 0);
  }

  static boolean finished(Context c, File f) {
    return prefs(c).getBoolean(f.getName() + ".done", false);
  }

  static boolean started(Context c, File f) {
    return position(c, f) > 0 || prefs(c).contains(f.getName() + ".played");
  }

  static void save(Context c, File f, long position, float speed) {
    if (!f.isFile()) return; // deleted while it played
    prefs(c)
        .edit()
        .putLong(f.getName(), Math.max(0, position))
        .putFloat(f.getName() + ".speed", speed)
        .apply();
  }

  static void played(Context c, File f) {
    if (!f.isFile()) return;
    prefs(c)
        .edit()
        .putLong(f.getName() + ".played", System.currentTimeMillis())
        .putBoolean(f.getName() + ".done", false)
        .putString("last", f.getAbsolutePath())
        .apply();
  }

  static void finish(Context c, File f) {
    if (!f.isFile()) return;
    prefs(c).edit().putLong(f.getName(), 0).putBoolean(f.getName() + ".done", true).apply();
  }

  static void saveDuration(Context c, File f, long duration) {
    if (duration > 0 && f.isFile() && duration != duration(c, f))
      prefs(c).edit().putLong(f.getName() + ".duration", duration).apply();
  }

  /** The book the listener had open last, if it is still in the library. */
  static File last(Context c) {
    String path = prefs(c).getString("last", "");
    File f = new File(path);
    return !path.isEmpty() && f.isFile() ? f : null;
  }

  static void setLast(Context c, File f) {
    prefs(c).edit().putString("last", f == null ? "" : f.getAbsolutePath()).apply();
  }

  static String title(Context c, File f) {
    String title = prefs(c).getString(f.getName() + ".title", "");
    return title.isEmpty() ? f.getName().replaceFirst("(?i)\\.(m4b|m4a|mp3)$", "") : title;
  }

  static String author(Context c, File f) {
    return prefs(c).getString(f.getName() + ".author", "");
  }

  static boolean hasDetails(Context c, File f) {
    return prefs(c).contains(f.getName() + ".meta");
  }

  /** Reads title, author and length from the file's tags once. Slow: call off the main thread. */
  static void readDetails(Context c, File f) {
    if (hasDetails(c, f)) return;
    String title = "", author = "";
    long duration = 0;
    MediaMetadataRetriever m = new MediaMetadataRetriever();
    try {
      m.setDataSource(f.getAbsolutePath());
      title = first(m, MediaMetadataRetriever.METADATA_KEY_TITLE, MediaMetadataRetriever.METADATA_KEY_ALBUM);
      author =
          first(
              m,
              MediaMetadataRetriever.METADATA_KEY_ARTIST,
              MediaMetadataRetriever.METADATA_KEY_ALBUMARTIST,
              MediaMetadataRetriever.METADATA_KEY_AUTHOR);
      String length = m.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION);
      if (length != null) duration = Long.parseLong(length);
    } catch (Exception ignored) {
      // An unreadable tag leaves the file name as the title.
    } finally {
      try {
        m.release();
      } catch (Exception ignored) {
      }
    }
    SharedPreferences.Editor e =
        prefs(c)
            .edit()
            .putString(f.getName() + ".title", title)
            .putString(f.getName() + ".author", author)
            .putBoolean(f.getName() + ".meta", true);
    if (duration > 0 && duration(c, f) == 0) e.putLong(f.getName() + ".duration", duration);
    e.apply();
  }

  static String first(MediaMetadataRetriever m, int... keys) {
    for (int key : keys) {
      String value = m.extractMetadata(key);
      if (value != null && !value.isBlank() && !value.equals("null")) return value.trim();
    }
    return "";
  }

  /** What the player, the notification and the lock screen show for this book. */
  static MediaItem item(Context c, File f) {
    if (!hasDetails(c, f)) readDetails(c, f);
    String title = title(c, f), author = author(c, f);
    return new MediaItem.Builder()
        .setMediaId(f.getAbsolutePath())
        .setUri(Uri.fromFile(f))
        .setMediaMetadata(
            new MediaMetadata.Builder()
                .setTitle(title)
                // Without an artist some phones print "null" under the title.
                .setArtist(author.isEmpty() ? "Audiobook" : author)
                .setAlbumTitle(title)
                .setMediaType(MediaMetadata.MEDIA_TYPE_AUDIO_BOOK)
                .setIsPlayable(true)
                .setIsBrowsable(false)
                .build())
        .build();
  }

  /** The embedded cover, scaled to about `size` pixels, or null. Slow: call off the main thread. */
  static Bitmap cover(File f, int size) {
    String key = f.getAbsolutePath() + "@" + size;
    Bitmap cached = covers.get(key);
    if (cached != null || noCover.contains(f.getAbsolutePath())) return cached;
    MediaMetadataRetriever m = new MediaMetadataRetriever();
    try {
      m.setDataSource(f.getAbsolutePath());
      byte[] art = m.getEmbeddedPicture();
      if (art == null || art.length > 16 * 1024 * 1024) {
        noCover.add(f.getAbsolutePath());
        return null;
      }
      BitmapFactory.Options bounds = new BitmapFactory.Options();
      bounds.inJustDecodeBounds = true;
      BitmapFactory.decodeByteArray(art, 0, art.length, bounds);
      BitmapFactory.Options options = new BitmapFactory.Options();
      options.inSampleSize = 1;
      while (Math.max(bounds.outWidth, bounds.outHeight) / (options.inSampleSize * 2) >= size)
        options.inSampleSize *= 2;
      Bitmap bitmap = BitmapFactory.decodeByteArray(art, 0, art.length, options);
      if (bitmap != null) covers.put(key, bitmap);
      else noCover.add(f.getAbsolutePath());
      return bitmap;
    } catch (Exception e) {
      return null;
    } finally {
      try {
        m.release();
      } catch (Exception ignored) {
      }
    }
  }

  /** Removes the book, its attached text and everything remembered about it. */
  static void delete(Context c, File f) {
    String name = f.getName();
    f.delete();
    new File(f.getAbsolutePath() + ".vtt").delete();
    SharedPreferences p = prefs(c);
    SharedPreferences.Editor e = p.edit();
    for (String key : p.getAll().keySet())
      if (key.equals(name) || key.startsWith(name + ".")) e.remove(key);
    if (f.getAbsolutePath().equals(p.getString("last", ""))) e.putString("last", "");
    e.apply();
    covers.evictAll();
  }
}
