package org.pdf2epub.listen;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.charset.StandardCharsets;
import org.junit.Test;

public class TranscriptTest {
  File text(String value) throws Exception {
    File f = File.createTempFile("captions", ".vtt");
    f.deleteOnExit();
    java.nio.file.Files.write(f.toPath(), value.getBytes(StandardCharsets.UTF_8));
    return f;
  }

  @Test
  public void readsHebrewAndMilliseconds() throws Exception {
    var result =
        Transcript.vtt(
            text(
                "WEBVTT\n\n"
                    + "00:00:01.250 --> 00:00:03.500\n"
                    + "שלום עולם\n\n"
                    + "00:00:04.000 --> 00:00:06.000\n"
                    + "Next paragraph\n"));
    assertEquals(2, result.size());
    assertEquals(1250, result.get(0).start());
    assertEquals("שלום עולם", result.get(0).text());
  }

  @Test(expected = IOException.class)
  public void rejectsOverlap() throws Exception {
    Transcript.vtt(
        text("00:00:01.000 --> 00:00:04.000\nOne\n\n00:00:02.000 --> 00:00:03.000\nTwo"));
  }

  @Test(expected = IOException.class)
  public void rejectsUntimedText() throws Exception {
    Transcript.vtt(text("Plain EPUB text cannot supply timings"));
  }

  @Test
  public void readsEmbeddedBox() throws Exception {
    File f = File.createTempFile("audiobook", ".m4b");
    f.deleteOnExit();
    byte[] data =
        "{\"version\":1,\"cues\":[{\"start\":0,\"end\":1000,\"text\":\"Hello\"}]}"
            .getBytes(StandardCharsets.UTF_8);
    try (DataOutputStream out = new DataOutputStream(new FileOutputStream(f))) {
      out.writeInt(24 + data.length);
      out.writeBytes("uuid");
      out.write(Transcript.ID);
      out.write(data);
    }
    assertEquals("Hello", Transcript.read(f).get(0).text());
  }
}
