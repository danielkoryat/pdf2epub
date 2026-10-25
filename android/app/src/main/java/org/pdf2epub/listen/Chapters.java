package org.pdf2epub.listen;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class Chapters {
  public record Chapter(String title, long start) {}

  public static List<Chapter> read(File file) throws IOException {
    try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
      List<Chapter> result = new ArrayList<>();
      walk(in, in.length(), result, 0);
      return result;
    }
  }

  static void walk(RandomAccessFile in, long end, List<Chapter> out, int depth) throws IOException {
    if (depth > 8) return;
    while (in.getFilePointer() + 8 <= end) {
      long start = in.getFilePointer();
      long size = Integer.toUnsignedLong(in.readInt());
      byte[] type = new byte[4];
      in.readFully(type);
      String tag = new String(type, StandardCharsets.US_ASCII);
      long header = 8;
      if (size == 1) {
        size = in.readLong();
        header = 16;
      }
      if (size == 0) size = end - start;
      if (size < header || size > end - start) return;
      long next = start + size;
      if (tag.equals("moov") || tag.equals("udta")) walk(in, next, out, depth + 1);
      else if (tag.equals("chpl") && size >= header + 9) {
        int version = in.readUnsignedByte();
        in.skipBytes(3);
        if (version == 1) in.skipBytes(4);
        int count = in.readUnsignedByte();
        for (int i = 0; i < count && in.getFilePointer() + 9 <= next; i++) {
          long time = in.readLong() / 10000;
          int length = in.readUnsignedByte();
          if (in.getFilePointer() + length > next) break;
          byte[] name = new byte[length];
          in.readFully(name);
          out.add(new Chapter(new String(name, StandardCharsets.UTF_8), time));
        }
      }
      in.seek(next);
    }
  }
}
