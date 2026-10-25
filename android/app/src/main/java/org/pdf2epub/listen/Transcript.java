package org.pdf2epub.listen;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.json.*;

final class Transcript {
  record Cue(long start, long end, String text) {}

  static final byte[] ID = hex("914bbd195f3b4b64a32fa44bb9cf8e11");

  static byte[] hex(String s) {
    byte[] b = new byte[s.length() / 2];
    for (int i = 0; i < b.length; i++)
      b[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
    return b;
  }

  static List<Cue> read(File book) throws Exception {
    File sidecar = new File(book.getAbsolutePath() + ".vtt");
    if (sidecar.isFile()) return vtt(sidecar);
    try (RandomAccessFile in = new RandomAccessFile(book, "r")) {
      while (in.getFilePointer() + 8 <= in.length()) {
        long start = in.getFilePointer(), size = Integer.toUnsignedLong(in.readInt());
        int type = in.readInt();
        long header = 8;
        if (size == 1) {
          size = in.readLong();
          header = 16;
        }
        if (size == 0) break;
        if (size < header || size > in.length() - start) break;
        if (type == 0x75756964 && size >= header + 16) {
          byte[] id = new byte[16];
          in.readFully(id);
          if (Arrays.equals(ID, id)) {
            long count = size - header - 16;
            if (count > 16 * 1024 * 1024) throw new IOException("Read-along text is too large");
            byte[] data = new byte[(int) count];
            in.readFully(data);
            JSONObject doc = new JSONObject(new String(data, StandardCharsets.UTF_8));
            if (doc.getInt("version") != 1) throw new IOException("Unsupported read-along version");
            JSONArray list = doc.getJSONArray("cues");
            List<Cue> cues = new ArrayList<>();
            long previous = 0;
            for (int i = 0; i < list.length(); i++) {
              JSONObject c = list.getJSONObject(i);
              long a = c.getLong("start"), b = c.getLong("end");
              if (a < previous || b < a) throw new IOException("Invalid read-along timings");
              cues.add(new Cue(a, b, c.getString("text")));
              previous = b;
            }
            return cues;
          }
        }
        in.seek(start + size);
      }
    }
    return Collections.emptyList();
  }

  static List<Cue> vtt(File path) throws Exception {
    if (path.length() > 16 * 1024 * 1024) throw new IOException("Timed text exceeds 16 MB");
    String source =
        new String(java.nio.file.Files.readAllBytes(path.toPath()), StandardCharsets.UTF_8)
            .replace("\r", "");
    java.util.regex.Pattern pattern =
        java.util.regex.Pattern.compile(
            "(?m)^([0-9:.]+) --> ([0-9:.]+)[^\n]*\n([\\s\\S]*?)(?=\n\n|\\z)");
    java.util.regex.Matcher match = pattern.matcher(source);
    List<Cue> result = new ArrayList<>();
    long previous = 0;
    while (match.find()) {
      long start = timestamp(match.group(1)), end = timestamp(match.group(2));
      if (start < previous || end <= start)
        throw new IOException("Overlapping or invalid cue times");
      String text = match.group(3).replaceAll("<[^>]*>", "");
      result.add(new Cue(start, end, text));
      previous = end;
    }
    if (result.isEmpty())
      throw new IOException("No timed captions found. Choose a WebVTT (.vtt) transcript.");
    return result;
  }

  static long timestamp(String value) {
    String[] parts = value.split(":");
    double seconds = 0;
    for (String part : parts) seconds = seconds * 60 + Double.parseDouble(part);
    return Math.round(seconds * 1000);
  }
}
