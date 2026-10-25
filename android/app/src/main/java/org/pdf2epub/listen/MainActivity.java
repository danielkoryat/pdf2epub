package org.pdf2epub.listen;

import android.app.*;
import android.content.*;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.*;
import android.net.Uri;
import android.os.*;
import android.text.TextUtils;
import android.view.*;
import android.widget.*;
import androidx.media3.common.*;
import androidx.media3.session.MediaController;
import androidx.media3.session.SessionToken;
import java.io.*;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
  static final int BG = 0xff0f1211, SURFACE = 0xff161a18, CARD = 0xff1b201d, RAISED = 0xff252c28;
  static final int GREEN = 0xff78efa3, GREEN_DIM = 0xff1f3a2a, TEXT = 0xfff2f5f1;
  static final int MUTED = 0xffa3aea7, FAINT = 0xff737f78, TRACK = 0xff313a35;
  static final int LIBRARY = 0, LISTEN = 1, READ = 2;
  static final float[] SPEEDS = {.75f, .9f, 1, 1.1f, 1.25f, 1.5f, 1.75f, 2, 2.5f, 3};
  static final int[] SLEEP_MINUTES = {5, 10, 15, 30, 45, 60, 90};
  static final String NO_TEXT =
      "This audiobook has no read-along text. Books made by the pdf2epub site include it. For"
          + " another book, attach a matching WebVTT (.vtt) transcript.";

  MediaController player;
  final BookImport imports = BookImport.INSTANCE;
  final Handler handler = new Handler(Looper.getMainLooper());
  final ExecutorService io = Executors.newSingleThreadExecutor();
  final Runnable ticker = this::tick;
  int tab = LIBRARY;
  boolean started;
  Runnable refresh = () -> {}; // updates the changing parts of the open screen

  FrameLayout body;
  FrameLayout[] navPills = new FrameLayout[3];
  ImageView[] navIcons = new ImageView[3];
  TextView[] navLabels = new TextView[3];

  LinearLayout importPanel;
  TextView importHeading, importDetail;
  ProgressBar importProgress;
  Button importAction, importPlay;
  File imported;

  LinearLayout mini;
  FrameLayout miniCover;
  TextView miniTitle, miniDetail;
  ImageView miniPlay;
  ProgressBar miniProgress;
  File miniBook;

  // The open book's chapters and read-along text, loaded in the background.
  File loaded;
  List<Chapters.Chapter> chapters = List.of();
  List<Transcript.Cue> cues = List.of();
  String textState = "";
  File textTarget;

  // Listen screen
  SeekBar seek;
  boolean dragging;
  long[] dragScope;
  int shownChapter = -2;
  Boolean shownPlaying;

  // Read screen
  boolean follow = true;
  int readIndex = -2;

  public void onCreate(Bundle state) {
    super.onCreate(state);
    if (state != null) tab = state.getInt("tab", LIBRARY);
    LinearLayout root = column();
    root.setBackgroundColor(BG);
    setContentView(root);
    importPanel = buildImportPanel();
    LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(-1, -2);
    ip.setMargins(dp(16), dp(8), dp(16), 0);
    root.addView(importPanel, ip);
    body = new FrameLayout(this);
    root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1));
    mini = buildMiniPlayer();
    LinearLayout.LayoutParams mp = new LinearLayout.LayoutParams(-1, -2);
    mp.setMargins(dp(10), dp(4), dp(10), dp(6));
    root.addView(mini, mp);
    LinearLayout nav = buildNav();
    root.addView(nav);
    root.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          root.setPadding(0, insets.getSystemWindowInsetTop(), 0, 0);
          nav.setPadding(0, dp(8), 0, insets.getSystemWindowInsetBottom() + dp(8));
          return insets;
        });
    root.requestApplyInsets();
    imports.observer = this::renderImport;
    renderImport();
    showTab(tab);
    var future =
        new MediaController.Builder(
                this, new SessionToken(this, new ComponentName(this, PlaybackService.class)))
            .buildAsync();
    future.addListener(
        () -> {
          try {
            player = future.get();
            if (isDestroyed()) {
              player.release();
              return;
            }
            player.addListener(
                new Player.Listener() {
                  public void onPlayerError(PlaybackException e) {
                    error("This book could not be played", e.getLocalizedMessage());
                  }

                  public void onEvents(Player p, Player.Events events) {
                    if (!Objects.equals(current(), loaded)) bookChanged();
                    refreshNow();
                  }
                });
            // Load the book the service reopened, so its length is known and it can be skipped through.
            if (player.getMediaItemCount() > 0 && player.getPlaybackState() == Player.STATE_IDLE)
              player.prepare();
            bookChanged();
            if (state == null && getIntent().getData() != null) importBook(getIntent().getData());
          } catch (Exception e) {
            error("Player unavailable", e.getMessage());
          }
        },
        this::runOnUiThread);
  }

  protected void onNewIntent(Intent intent) {
    super.onNewIntent(intent);
    if (intent.getData() != null) importBook(intent.getData());
  }

  protected void onSaveInstanceState(Bundle state) {
    super.onSaveInstanceState(state);
    state.putInt("tab", tab);
  }

  protected void onStart() {
    super.onStart();
    started = true;
    handler.removeCallbacks(ticker);
    tick();
  }

  protected void onStop() {
    started = false;
    handler.removeCallbacks(ticker);
    super.onStop();
  }

  public void onBackPressed() {
    if (tab != LIBRARY) showTab(LIBRARY);
    else super.onBackPressed();
  }

  protected void onDestroy() {
    handler.removeCallbacksAndMessages(null);
    imports.observer = null;
    io.shutdownNow();
    if (player != null) player.release();
    super.onDestroy();
  }

  void tick() {
    if (!started || isDestroyed()) return;
    renderImport();
    refreshNow();
    handler.postDelayed(ticker, 250);
  }

  void refreshNow() {
    renderMini();
    refresh.run();
  }

  // -- the player ---------------------------------------------------------------------------------

  File current() {
    if (player == null) return null;
    MediaItem item = player.getCurrentMediaItem();
    return item == null || item.mediaId.isEmpty() ? null : new File(item.mediaId);
  }

  boolean playing() {
    if (player == null) return false;
    int state = player.getPlaybackState();
    return player.getPlayWhenReady() && state != Player.STATE_IDLE && state != Player.STATE_ENDED;
  }

  long position() {
    return player == null ? 0 : Math.max(0, player.getCurrentPosition());
  }

  long duration() {
    long d = player == null ? 0 : player.getDuration();
    if (d > 0) return d;
    File book = current();
    return book == null ? 0 : Books.duration(this, book);
  }

  float speed() {
    return player == null ? 1 : player.getPlaybackParameters().speed;
  }

  void togglePlay() {
    if (player == null || current() == null) return;
    if (playing()) {
      player.pause();
    } else {
      if (player.getPlaybackState() == Player.STATE_IDLE) player.prepare();
      if (player.getPlaybackState() == Player.STATE_ENDED) player.seekTo(0);
      player.play();
    }
    refreshNow();
  }

  void play(File f, boolean fromStart) {
    if (player == null) return;
    File now = current();
    if (f.equals(now)) {
      if (fromStart) player.seekTo(0);
      if (!playing()) togglePlay();
    } else {
      if (now != null) Books.save(this, now, player.getCurrentPosition(), speed());
      long start = fromStart || Books.finished(this, f) ? 0 : Books.position(this, f);
      player.setMediaItem(Books.item(this, f), start);
      player.setPlaybackSpeed(Books.speed(this, f));
      player.prepare();
      player.play();
    }
    showTab(LISTEN);
  }

  void seekTo(long ms) {
    File book = current();
    if (player == null || book == null) return;
    long d = duration(), target = Math.max(0, d > 0 ? Math.min(ms, d - 1000) : ms);
    // Until the book is loaded the player refuses seeks; reopen it at the new place instead.
    if (player.isCommandAvailable(Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM)) player.seekTo(target);
    else player.setMediaItem(Books.item(this, book), target);
    refreshNow();
  }

  void bookChanged() {
    File book = current();
    loaded = book;
    chapters = List.of();
    cues = List.of();
    textState = "Loading the text…";
    shownChapter = -2;
    readIndex = -2;
    if (tab != LIBRARY) showTab(tab);
    if (book == null) return;
    io.execute(
        () -> {
          List<Chapters.Chapter> marks;
          try {
            marks = Chapters.read(book);
          } catch (Exception e) {
            marks = List.of();
          }
          List<Transcript.Cue> text;
          String state;
          try {
            text = Transcript.read(book);
            state = text.isEmpty() ? NO_TEXT : "";
          } catch (Exception e) {
            text = List.of();
            state = "Could not read this book's text: " + e.getMessage();
          }
          List<Chapters.Chapter> foundMarks = marks;
          List<Transcript.Cue> foundText = text;
          String foundState = state;
          runOnUiThread(
              () -> {
                if (isDestroyed() || !book.equals(loaded)) return;
                chapters = foundMarks;
                cues = foundText;
                textState = foundState;
                if (tab != LIBRARY) showTab(tab);
              });
        });
  }

  /** The chapter playing at `ms`, or -1 before the first chapter mark. */
  int chapterAt(long ms) {
    int index = -1;
    for (int i = 0; i < chapters.size(); i++) if (chapters.get(i).start() <= ms) index = i;
    return index;
  }

  /** What the progress bar spans: the current chapter, or the whole book. */
  long[] scope() {
    long d = Math.max(duration(), 1);
    if (!bookScope() && !chapters.isEmpty()) {
      int i = chapterAt(position());
      long start = i < 0 ? 0 : chapters.get(i).start();
      long end = i + 1 < chapters.size() ? chapters.get(i + 1).start() : d;
      if (end > start) return new long[] {start, end};
    }
    return new long[] {0, d};
  }

  boolean bookScope() {
    return getSharedPreferences("settings", 0).getBoolean("bookScope", false);
  }

  void previousChapter() {
    int i = chapterAt(position());
    if (i < 0) return;
    // Like a music player: back to the start of this chapter, or to the previous one when at its start.
    long start = chapters.get(i).start();
    seekTo(position() - start > 3000 || i == 0 ? start : chapters.get(i - 1).start());
  }

  void nextChapter() {
    int i = chapterAt(position());
    if (i + 1 < chapters.size()) seekTo(chapters.get(i + 1).start());
  }

  // -- screens ------------------------------------------------------------------------------------

  void showTab(int t) {
    tab = t;
    for (int i = 0; i < 3; i++) {
      boolean on = i == t;
      navPills[i].setBackground(on ? shape(GREEN_DIM, 16) : null);
      navIcons[i].setImageTintList(ColorStateList.valueOf(on ? GREEN : MUTED));
      navLabels[i].setTextColor(on ? TEXT : MUTED);
      navLabels[i].setTypeface(null, on ? Typeface.BOLD : Typeface.NORMAL);
    }
    body.removeAllViews();
    refresh = () -> {};
    if (t == LIBRARY) buildLibrary();
    else if (t == LISTEN) buildListen();
    else buildRead();
    refreshNow();
  }

  void buildLibrary() {
    ScrollView scroll = new ScrollView(this);
    LinearLayout page = column();
    page.setPadding(dp(20), dp(12), dp(20), dp(24));
    scroll.addView(page);
    body.addView(scroll);
    List<File> books = Books.list(this);
    LinearLayout header = row();
    LinearLayout heading = column();
    TextView title = text("Library", 30, TEXT);
    title.setTypeface(Typeface.create("serif", Typeface.BOLD));
    heading.addView(title);
    heading.addView(
        text(
            books.isEmpty()
                ? "No books yet"
                : books.size() + (books.size() == 1 ? " audiobook" : " audiobooks"),
            14,
            MUTED));
    header.addView(heading, new LinearLayout.LayoutParams(0, -2, 1));
    if (!books.isEmpty()) header.addView(iconButton(R.drawable.ic_add, "Add audiobook", this::add, 48, true));
    page.addView(header);
    if (books.isEmpty()) {
      page.addView(
          emptyState(
              R.drawable.ic_book,
              "Your library is empty",
              "Add an M4B, M4A or MP3 audiobook from Google Drive or your phone. It is saved"
                  + " here, so you can listen offline.",
              "Add audiobook",
              this::add));
      return;
    }
    File resume = current() != null ? current() : Books.last(this);
    if (resume != null && resume.isFile()) {
      page.addView(eyebrow("CONTINUE LISTENING"));
      page.addView(continueCard(resume));
    }
    page.addView(eyebrow("ALL BOOKS"));
    List<File> unread = new ArrayList<>();
    for (File f : books) {
      page.addView(bookRow(f));
      if (!Books.hasDetails(this, f)) unread.add(f);
    }
    if (!unread.isEmpty())
      io.execute(
          () -> {
            for (File f : unread) Books.readDetails(this, f);
            runOnUiThread(
                () -> {
                  if (!isDestroyed() && tab == LIBRARY) showTab(LIBRARY);
                });
          });
  }

  View continueCard(File f) {
    LinearLayout card = row();
    card.setPadding(dp(14), dp(14), dp(14), dp(14));
    card.setBackground(ripple(shape(CARD, 20)));
    card.addView(cover(f, 76, 108, 10));
    LinearLayout details = column();
    details.setPadding(dp(14), 0, dp(10), 0);
    card.addView(details, new LinearLayout.LayoutParams(0, -2, 1));
    TextView name = label(Books.title(this, f), 18, TEXT);
    name.setTypeface(Typeface.create("serif", Typeface.BOLD));
    name.setMaxLines(2);
    name.setEllipsize(TextUtils.TruncateAt.END);
    details.addView(name);
    String author = Books.author(this, f);
    if (!author.isEmpty()) details.addView(rowLine(author, 13, MUTED));
    ProgressBar bar = progressLine(6);
    LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(-1, dp(4));
    bp.setMargins(0, dp(12), 0, dp(6));
    details.addView(bar, bp);
    TextView status = text("", 12, MUTED);
    details.addView(status);
    ImageView button = iconButton(R.drawable.ic_play, "Play", () -> {
      if (f.equals(current())) togglePlay();
      else play(f, false);
    }, 56, true);
    card.addView(button);
    card.setOnClickListener(v -> {
      if (f.equals(current())) showTab(LISTEN);
      else play(f, false);
    });
    card.setContentDescription("Continue " + Books.title(this, f));
    refresh =
        () -> {
          boolean now = f.equals(current());
          long d = now ? duration() : Books.duration(this, f);
          long p = now ? position() : Books.position(this, f);
          bar.setMax(1000);
          bar.setProgress(d > 0 ? (int) (p * 1000 / d) : 0);
          status.setText(
              d > 0 ? span(d - p) + " left  ·  " + (p * 100 / d) + "%" : "Ready to continue");
          boolean on = now && playing();
          button.setImageResource(on ? R.drawable.ic_pause : R.drawable.ic_play);
          button.setContentDescription(on ? "Pause" : "Play");
        };
    LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(-1, -2);
    cp.bottomMargin = dp(8);
    card.setLayoutParams(cp);
    return card;
  }

  View bookRow(File f) {
    LinearLayout row = row();
    row.setPadding(dp(8), dp(10), dp(2), dp(10));
    row.setBackground(ripple(shape(BG, 14)));
    row.addView(cover(f, 52, 74, 8));
    LinearLayout details = column();
    details.setPadding(dp(14), 0, dp(4), 0);
    row.addView(details, new LinearLayout.LayoutParams(0, -2, 1));
    TextView name = label(Books.title(this, f), 16, TEXT);
    name.setMaxLines(2);
    name.setEllipsize(TextUtils.TruncateAt.END);
    details.addView(name);
    String author = Books.author(this, f);
    if (!author.isEmpty()) details.addView(rowLine(author, 13, MUTED));
    long d = Books.duration(this, f), p = Books.position(this, f);
    boolean done = Books.finished(this, f), begun = Books.started(this, f) && !done;
    String status =
        done || (begun && d > 0 && d - p < 2000)
            ? "Finished"
            : begun && d > 0
                ? (p * 100 / d) + "%  ·  " + span(d - p) + " left"
                : (d > 0 ? span(d) + "  ·  " : "") + "New";
    if (begun && d > 0 && !status.equals("Finished")) {
      ProgressBar bar = progressLine(4);
      bar.setMax(1000);
      bar.setProgress((int) (p * 1000 / d));
      LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(dp(120), dp(3));
      bp.setMargins(0, dp(8), 0, 0);
      details.addView(bar, bp);
    }
    TextView info = text(status, 12, done || !begun ? FAINT : GREEN);
    info.setPadding(0, dp(4), 0, 0);
    details.addView(info);
    ImageView more = iconButton(R.drawable.ic_more, "More for " + Books.title(this, f), null, 44, false);
    more.setOnClickListener(v -> bookMenu(f, more));
    row.addView(more);
    row.setOnClickListener(v -> play(f, false));
    row.setOnLongClickListener(
        v -> {
          bookMenu(f, more);
          return true;
        });
    row.setContentDescription("Play " + Books.title(this, f));
    return row;
  }

  void bookMenu(File f, View anchor) {
    PopupMenu menu = new PopupMenu(this, anchor);
    menu.getMenu().add(0, 1, 0, f.equals(current()) && playing() ? "Open player" : "Play");
    if (Books.started(this, f)) menu.getMenu().add(0, 2, 1, "Play from the beginning");
    menu.getMenu().add(0, 3, 2, "Delete from library");
    menu.setOnMenuItemClickListener(
        item -> {
          if (item.getItemId() == 1) play(f, false);
          else if (item.getItemId() == 2) play(f, true);
          else delete(f);
          return true;
        });
    menu.show();
  }

  void delete(File f) {
    String title = Books.title(this, f);
    new AlertDialog.Builder(this)
        .setTitle("Delete this audiobook?")
        .setMessage(
            "\"" + title + "\" and your place in it are removed from this phone. The original"
                + " file in Google Drive or your files stays where it is.")
        .setPositiveButton(
            "Delete",
            (d, w) -> {
              if (player != null && f.equals(current())) {
                player.stop();
                player.clearMediaItems();
              }
              Books.delete(this, f);
              Toast.makeText(this, "Deleted " + title, Toast.LENGTH_SHORT).show();
              showTab(LIBRARY);
            })
        .setNegativeButton("Cancel", null)
        .show();
  }

  void buildListen() {
    File book = current();
    if (book == null) {
      boolean empty = Books.list(this).isEmpty();
      body.addView(
          emptyState(
              R.drawable.ic_listen,
              "Nothing playing",
              empty
                  ? "Add an audiobook to start listening."
                  : "Choose a book from your library to start listening.",
              empty ? "Add audiobook" : "Open library",
              empty ? this::add : () -> showTab(LIBRARY)));
      return;
    }
    ScrollView scroll = new ScrollView(this);
    LinearLayout page = column();
    page.setGravity(Gravity.CENTER_HORIZONTAL);
    page.setPadding(dp(24), dp(16), dp(24), dp(16));
    scroll.addView(page);
    body.addView(scroll);
    android.util.DisplayMetrics m = getResources().getDisplayMetrics();
    int width =
        (int)
            Math.min(
                Math.min(m.widthPixels / m.density - 140, m.heightPixels / m.density * .26f), 260);
    page.addView(bigCover(book, width));
    TextView name = text(Books.title(this, book), 22, TEXT);
    name.setTypeface(Typeface.create("serif", Typeface.BOLD));
    name.setGravity(Gravity.CENTER);
    name.setMaxLines(2);
    name.setEllipsize(TextUtils.TruncateAt.END);
    name.setPadding(0, dp(18), 0, 0);
    page.addView(name, new LinearLayout.LayoutParams(-1, -2));
    String author = Books.author(this, book);
    if (!author.isEmpty()) {
      TextView by = oneLine(author, 14, MUTED);
      by.setGravity(Gravity.CENTER);
      page.addView(by, new LinearLayout.LayoutParams(-1, -2));
    }
    TextView chapter = text("", 14, GREEN);
    chapter.setGravity(Gravity.CENTER);
    chapter.setSingleLine(true);
    chapter.setEllipsize(TextUtils.TruncateAt.END);
    chapter.setPadding(dp(14), dp(7), dp(14), dp(7));
    chapter.setBackground(ripple(shape(GREEN_DIM, 18)));
    chapter.setOnClickListener(v -> chapterList());
    LinearLayout.LayoutParams chp = new LinearLayout.LayoutParams(-2, -2);
    chp.setMargins(0, dp(12), 0, 0);
    page.addView(chapter, chp);
    chapter.setVisibility(chapters.isEmpty() ? View.GONE : View.VISIBLE);

    seek = new SeekBar(this);
    styleSeekBar(seek);
    seek.setContentDescription("Playback position");
    LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(-1, dp(48));
    sp.setMargins(-dp(4), dp(14), -dp(4), 0);
    page.addView(seek, sp);
    LinearLayout times = row();
    TextView elapsed = text("0:00", 13, MUTED);
    TextView remaining = text("0:00", 13, MUTED);
    remaining.setGravity(Gravity.END);
    elapsed.setOnClickListener(v -> goToTime());
    remaining.setOnClickListener(v -> goToTime());
    // Switches the bar between this chapter and the whole book.
    TextView scopeToggle = text("", 12, MUTED);
    scopeToggle.setPadding(dp(12), dp(6), dp(12), dp(6));
    scopeToggle.setBackground(ripple(shape(RAISED, 14)));
    scopeToggle.setOnClickListener(
        v -> {
          getSharedPreferences("settings", 0).edit().putBoolean("bookScope", !bookScope()).apply();
          refreshNow();
        });
    scopeToggle.setVisibility(chapters.isEmpty() ? View.GONE : View.VISIBLE);
    times.addView(elapsed, new LinearLayout.LayoutParams(0, -2, 1));
    times.addView(scopeToggle);
    times.addView(remaining, new LinearLayout.LayoutParams(0, -2, 1));
    page.addView(times, new LinearLayout.LayoutParams(-1, -2));
    TextView where = text("", 12, FAINT);
    where.setGravity(Gravity.CENTER);
    where.setPadding(0, dp(6), 0, 0);
    page.addView(where, new LinearLayout.LayoutParams(-1, -2));
    seek.setOnSeekBarChangeListener(
        new SeekBar.OnSeekBarChangeListener() {
          public void onStartTrackingTouch(SeekBar s) {
            dragging = true;
            dragScope = scope();
          }

          public void onStopTrackingTouch(SeekBar s) {
            dragging = false;
            seekTo(dragScope[0] + s.getProgress());
          }

          public void onProgressChanged(SeekBar s, int p, boolean user) {
            if (!dragging) return;
            // While dragging: where the book will continue, and how far that is from now.
            long target = dragScope[0] + p, jump = target - position();
            elapsed.setText(clock(target));
            elapsed.setTextColor(GREEN);
            remaining.setText((jump < 0 ? "-" : "+") + clock(Math.abs(jump)));
            remaining.setTextColor(GREEN);
          }
        });

    LinearLayout controls = row();
    controls.setGravity(Gravity.CENTER);
    LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(-1, -2);
    cp.setMargins(0, dp(14), 0, 0);
    page.addView(controls, cp);
    ImageView previous = iconButton(R.drawable.ic_previous, "Previous chapter", this::previousChapter, 52, false);
    View back = skipButton(R.drawable.ic_back, "15", "Back 15 seconds", () -> seekTo(position() - 15000));
    ImageView playButton = iconButton(R.drawable.ic_play, "Play", this::togglePlay, 76, true);
    View forward = skipButton(R.drawable.ic_forward, "30", "Forward 30 seconds", () -> seekTo(position() + 30000));
    ImageView next = iconButton(R.drawable.ic_next, "Next chapter", this::nextChapter, 52, false);
    for (View v : new View[] {previous, back, playButton, forward, next}) {
      LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) v.getLayoutParams();
      lp.setMargins(dp(5), 0, dp(5), 0);
      controls.addView(v, lp);
    }
    boolean marks = chapters.size() > 1;
    previous.setEnabled(marks);
    next.setEnabled(marks);
    previous.setAlpha(marks ? 1 : .3f);
    next.setAlpha(marks ? 1 : .3f);

    LinearLayout tools = row();
    tools.setGravity(Gravity.CENTER);
    LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(-1, -2);
    tp.setMargins(0, dp(20), 0, 0);
    page.addView(tools, tp);
    TextView speedChip = chip(R.drawable.ic_speed, "1×", this::chooseSpeed);
    TextView sleepChip = chip(R.drawable.ic_timer, "Sleep", this::chooseSleep);
    TextView chaptersChip = chip(R.drawable.ic_chapters, "Chapters", this::chapterList);
    TextView scopeChip = chip(R.drawable.ic_goto, "Go to", this::goToTime);
    for (TextView c : new TextView[] {speedChip, sleepChip, chaptersChip, scopeChip}) {
      // Icon above its label, so four fit side by side on a narrow phone.
      Drawable icon = c.getCompoundDrawablesRelative()[0];
      c.setCompoundDrawablesRelative(null, icon, null, null);
      c.setCompoundDrawablePadding(dp(4));
      c.setTextSize(12);
      c.setPadding(dp(4), dp(8), dp(4), dp(8));
      LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(64), 1);
      lp.setMargins(dp(3), 0, dp(3), 0);
      tools.addView(c, lp);
    }
    chaptersChip.setVisibility(chapters.isEmpty() ? View.GONE : View.VISIBLE);
    shownChapter = -2;
    shownPlaying = null;
    refresh =
        () -> {
          long pos = position(), d = duration();
          int ci = chapterAt(pos);
          if (ci != shownChapter) {
            shownChapter = ci;
            chapter.setText(ci < 0 ? "Opening" : chapters.get(ci).title() + "  ▾");
            chapter.setContentDescription("Chapters. Now playing: " + chapter.getText());
          }
          boolean on = playing();
          if (!Boolean.valueOf(on).equals(shownPlaying)) {
            shownPlaying = on;
            playButton.setImageResource(on ? R.drawable.ic_pause : R.drawable.ic_play);
            playButton.setContentDescription(on ? "Pause" : "Play");
          }
          if (!dragging) {
            long[] s = scope();
            seek.setMax((int) (s[1] - s[0]));
            seek.setProgress((int) (pos - s[0]));
            elapsed.setText(clock(pos - s[0]));
            remaining.setText("-" + clock(Math.max(0, s[1] - pos)));
            elapsed.setTextColor(MUTED);
            remaining.setTextColor(MUTED);
          }
          String left = d > 0 ? span(Math.max(0, d - pos)) + " left in the book" : "";
          where.setText(
              !chapters.isEmpty() && !bookScope() && ci >= 0
                  ? "Chapter " + (ci + 1) + " of " + chapters.size() + (left.isEmpty() ? "" : "  ·  " + left)
                  : d > 0 ? clock(pos) + " of " + clock(d) + "  ·  " + left : "");
          scopeToggle.setText(bookScope() ? "Whole book  ⇄" : "Chapter  ⇄");
          scopeToggle.setContentDescription(
              bookScope() ? "The bar shows the whole book. Show this chapter" : "The bar shows this chapter. Show the whole book");
          speedChip.setText(speedLabel(speed()));
          long sleepLeft = PlaybackService.sleepAt - SystemClock.elapsedRealtime();
          sleepChip.setText(
              PlaybackService.sleepAt > 0
                  ? clock(Math.max(0, sleepLeft))
                  : PlaybackService.sleepAtPosition >= 0 ? "Chapter end" : "Sleep");
          sleepChip.setTextColor(
              PlaybackService.sleepAt > 0 || PlaybackService.sleepAtPosition >= 0 ? GREEN : TEXT);
        };
  }

  void buildRead() {
    File book = current();
    if (book == null) {
      body.addView(
          emptyState(
              R.drawable.ic_read,
              "Nothing to read yet",
              "Start a book from your library, then follow its text here while you listen.",
              "Open library",
              () -> showTab(LIBRARY)));
      return;
    }
    if (cues.isEmpty()) {
      ScrollView scroll = new ScrollView(this);
      LinearLayout page = column();
      page.setPadding(dp(24), dp(24), dp(24), dp(24));
      scroll.addView(page);
      body.addView(scroll);
      TextView title = text("Read along", 26, TEXT);
      title.setTypeface(Typeface.create("serif", Typeface.BOLD));
      page.addView(title);
      TextView state = text(textState, 16, MUTED);
      state.setPadding(0, dp(10), 0, dp(20));
      state.setLineSpacing(dp(3), 1);
      page.addView(state);
      if (textState.equals(NO_TEXT) || textState.startsWith("Could not"))
        page.addView(
            button(
                "Attach timed text (.vtt)",
                () -> {
                  textTarget = book;
                  startActivityForResult(
                      new Intent(Intent.ACTION_OPEN_DOCUMENT)
                          .setType("*/*")
                          .addCategory(Intent.CATEGORY_OPENABLE),
                      2);
                },
                true));
      return;
    }
    LinearLayout page = column();
    body.addView(page);
    LinearLayout header = row();
    header.setPadding(dp(20), dp(8), dp(8), dp(4));
    TextView title = rowLine(Books.title(this, book), 15, MUTED);
    title.setPaddingRelative(0, 0, dp(12), 0);
    header.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
    TextView size = text("Aa", 17, TEXT);
    size.setGravity(Gravity.CENTER);
    size.setBackground(ripple(shape(RAISED, 20)));
    size.setContentDescription("Text size");
    header.addView(size, new LinearLayout.LayoutParams(dp(52), dp(40)));
    page.addView(header);
    FrameLayout frame = new FrameLayout(this);
    page.addView(frame, new LinearLayout.LayoutParams(-1, 0, 1));
    ListView list = new ListView(this);
    list.setDivider(null);
    list.setSelector(new ColorDrawable(Color.TRANSPARENT));
    list.setClipToPadding(false);
    list.setPadding(dp(12), dp(8), dp(12), dp(96));
    list.setVerticalScrollBarEnabled(true);
    list.setFastScrollEnabled(cues.size() > 200);
    frame.addView(list);
    TextView follow = chip(R.drawable.ic_follow, "Back to now", null);
    follow.setTextColor(BG);
    follow.setCompoundDrawableTintList(ColorStateList.valueOf(BG));
    follow.setBackground(ripple(shape(GREEN, 22)));
    follow.setPadding(dp(18), 0, dp(18), 0);
    follow.setVisibility(View.GONE);
    FrameLayout.LayoutParams fp = new FrameLayout.LayoutParams(-2, dp(44), Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
    fp.bottomMargin = dp(16);
    frame.addView(follow, fp);
    CueAdapter adapter = new CueAdapter();
    list.setAdapter(adapter);
    this.follow = true;
    readIndex = cueAt(position());
    list.setSelectionFromTop(Math.max(0, readIndex), dp(120));
    Runnable backToNow =
        () -> {
          this.follow = true;
          follow.setVisibility(View.GONE);
          if (readIndex >= 0) list.smoothScrollToPositionFromTop(readIndex, dp(120), 300);
        };
    follow.setOnClickListener(v -> backToNow.run());
    list.setOnScrollListener(
        new AbsListView.OnScrollListener() {
          public void onScrollStateChanged(AbsListView v, int state) {
            if (state == SCROLL_STATE_TOUCH_SCROLL) {
              MainActivity.this.follow = false;
              follow.setVisibility(View.VISIBLE);
            }
          }

          public void onScroll(AbsListView v, int first, int count, int total) {}
        });
    list.setOnItemClickListener(
        (p, v, i, id) -> {
          readIndex = i;
          adapter.notifyDataSetChanged();
          seekTo(cues.get(i).start());
          if (!playing()) togglePlay();
          backToNow.run();
        });
    size.setOnClickListener(
        v -> {
          int[] sizes = {16, 19, 22, 26};
          int now = readSize(), next = sizes[0];
          for (int i = 0; i < sizes.length; i++) if (sizes[i] == now) next = sizes[(i + 1) % sizes.length];
          getSharedPreferences("settings", 0).edit().putInt("readSize", next).apply();
          adapter.notifyDataSetChanged();
        });
    refresh =
        () -> {
          int i = cueAt(position());
          if (i == readIndex) return;
          readIndex = i;
          adapter.notifyDataSetChanged();
          if (this.follow && i >= 0) list.smoothScrollToPositionFromTop(i, dp(120), 400);
        };
  }

  int readSize() {
    return getSharedPreferences("settings", 0).getInt("readSize", 19);
  }

  /** The passage being read at `ms`; between passages, the one just read. */
  int cueAt(long ms) {
    int lo = 0, hi = cues.size() - 1, found = -1;
    while (lo <= hi) {
      int mid = (lo + hi) >>> 1;
      if (cues.get(mid).start() <= ms) {
        found = mid;
        lo = mid + 1;
      } else hi = mid - 1;
    }
    return found;
  }

  class CueAdapter extends BaseAdapter {
    public int getCount() {
      return cues.size();
    }

    public Object getItem(int i) {
      return cues.get(i);
    }

    public long getItemId(int i) {
      return i;
    }

    public View getView(int i, View reuse, ViewGroup parent) {
      TextView t = (TextView) reuse;
      if (t == null) {
        t = new TextView(MainActivity.this);
        t.setTypeface(Typeface.create("serif", Typeface.NORMAL));
        t.setTextDirection(View.TEXT_DIRECTION_FIRST_STRONG);
        t.setLineSpacing(dp(4), 1.05f);
        t.setPadding(dp(14), dp(10), dp(14), dp(10));
      }
      t.setText(cues.get(i).text());
      t.setTextSize(readSize());
      t.setTextColor(i == readIndex ? TEXT : i < readIndex ? FAINT : 0xffd0d7d2);
      t.setBackground(i == readIndex ? shape(GREEN_DIM, 14) : null);
      return t;
    }
  }

  // -- dialogs ------------------------------------------------------------------------------------

  void add() {
    if (imports.busy) {
      Toast.makeText(this, "Please wait for the current import", Toast.LENGTH_SHORT).show();
      return;
    }
    new AlertDialog.Builder(this)
        .setTitle("Add an audiobook")
        .setItems(
            new String[] {"From Google Drive", "From files on this phone"},
            (d, w) -> {
              if (w == 0)
                Toast.makeText(
                        this,
                        "In the file picker, open the menu (☰) and choose Drive",
                        Toast.LENGTH_LONG)
                    .show();
              startActivityForResult(
                  new Intent(Intent.ACTION_OPEN_DOCUMENT)
                      .setType("*/*")
                      .addCategory(Intent.CATEGORY_OPENABLE),
                  1);
            })
        .setNegativeButton("Cancel", null)
        .show();
  }

  void chooseSpeed() {
    if (player == null) return;
    String[] labels = new String[SPEEDS.length];
    int checked = 2;
    for (int i = 0; i < SPEEDS.length; i++) {
      labels[i] = speedLabel(SPEEDS[i]);
      if (Math.abs(SPEEDS[i] - speed()) < .01) checked = i;
    }
    new AlertDialog.Builder(this)
        .setTitle("Playback speed")
        .setSingleChoiceItems(
            labels,
            checked,
            (d, w) -> {
              player.setPlaybackSpeed(SPEEDS[w]);
              d.dismiss();
              refreshNow();
            })
        .show();
  }

  void chooseSleep() {
    List<String> labels = new ArrayList<>();
    labels.add("Off");
    for (int m : SLEEP_MINUTES) labels.add(m < 60 || m % 60 != 0 ? m + " minutes" : m / 60 + " hour");
    if (!chapters.isEmpty()) labels.add("End of this chapter");
    new AlertDialog.Builder(this)
        .setTitle("Sleep timer")
        .setItems(
            labels.toArray(new String[0]),
            (d, w) -> {
              PlaybackService.sleepAt = 0;
              PlaybackService.sleepAtPosition = -1;
              if (w >= 1 && w <= SLEEP_MINUTES.length)
                PlaybackService.sleepAt =
                    SystemClock.elapsedRealtime() + SLEEP_MINUTES[w - 1] * 60000L;
              else if (w > SLEEP_MINUTES.length) {
                int i = chapterAt(position());
                PlaybackService.sleepAtPosition =
                    i + 1 < chapters.size() ? chapters.get(i + 1).start() - 300 : Math.max(0, duration() - 300);
              }
              refreshNow();
            })
        .show();
  }

  void chapterList() {
    if (chapters.isEmpty()) return;
    String[] names = new String[chapters.size()];
    for (int i = 0; i < names.length; i++)
      names[i] = chapters.get(i).title() + "   ·   " + clock(chapters.get(i).start());
    int now = chapterAt(position());
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("Chapters")
            .setSingleChoiceItems(
                names,
                now,
                (d, w) -> {
                  seekTo(chapters.get(w).start());
                  d.dismiss();
                })
            .setNegativeButton("Close", null)
            .show();
    if (now > 2) dialog.getListView().setSelection(now - 2);
  }

  /** Jump to an exact time: dragging is too coarse in a long book. */
  void goToTime() {
    if (current() == null) return;
    long pos = position(), d = duration();
    LinearLayout pickers = row();
    pickers.setGravity(Gravity.CENTER);
    pickers.setPadding(0, dp(16), 0, 0);
    NumberPicker hours = picker(pickers, "hours", (int) Math.max(0, d / 3600000), (int) (pos / 3600000));
    NumberPicker minutes = picker(pickers, "min", 59, (int) (pos / 60000 % 60));
    NumberPicker seconds = picker(pickers, "sec", 59, (int) (pos / 1000 % 60));
    new AlertDialog.Builder(this)
        .setTitle("Go to time")
        .setMessage(d > 0 ? "The book is " + clock(d) + " long." : null)
        .setView(pickers)
        .setPositiveButton(
            "Go",
            (x, y) ->
                seekTo(
                    hours.getValue() * 3600000L
                        + minutes.getValue() * 60000L
                        + seconds.getValue() * 1000L))
        .setNegativeButton("Cancel", null)
        .show();
  }

  NumberPicker picker(LinearLayout parent, String label, int max, int value) {
    LinearLayout box = column();
    box.setGravity(Gravity.CENTER_HORIZONTAL);
    NumberPicker p = new NumberPicker(this);
    p.setMinValue(0);
    p.setMaxValue(max);
    p.setValue(Math.min(max, value));
    p.setFormatter(n -> label.equals("hours") ? String.valueOf(n) : String.format(Locale.US, "%02d", n));
    box.addView(p);
    TextView name = text(label, 12, MUTED);
    name.setGravity(Gravity.CENTER);
    box.addView(name, new LinearLayout.LayoutParams(-1, -2));
    LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2);
    lp.setMargins(dp(10), 0, dp(10), 0);
    parent.addView(box, lp);
    return p;
  }

  // -- import -------------------------------------------------------------------------------------

  protected void onActivityResult(int r, int c, Intent data) {
    super.onActivityResult(r, c, data);
    if (r == 1 && c == RESULT_OK && data != null) importBook(data.getData());
    if (r == 2 && c == RESULT_OK && data != null) attachText(data.getData());
  }

  void importBook(Uri uri) {
    imports.start(this, uri);
    renderImport();
  }

  LinearLayout buildImportPanel() {
    LinearLayout panel = column();
    panel.setPadding(dp(16), dp(12), dp(16), dp(10));
    panel.setBackground(shape(GREEN_DIM, 16));
    importHeading = text("", 15, TEXT);
    importHeading.setTypeface(null, Typeface.BOLD);
    importDetail = text("", 13, MUTED);
    importDetail.setPadding(0, dp(4), 0, 0);
    importProgress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
    importProgress.setProgressTintList(ColorStateList.valueOf(GREEN));
    importAction =
        flatButton(
            "Cancel",
            () -> {
              if (imports.busy) imports.cancel();
              else {
                imports.message = "";
                imported = null;
                renderImport();
              }
            });
    importPlay =
        flatButton(
            "Play now",
            () -> {
              File f = imported;
              imports.message = "";
              imported = null;
              renderImport();
              if (f != null && f.isFile()) play(f, false);
            });
    importPlay.setTextColor(GREEN);
    LinearLayout actions = row();
    actions.setGravity(Gravity.END);
    actions.addView(importAction);
    actions.addView(importPlay);
    panel.addView(importHeading);
    panel.addView(importDetail);
    panel.addView(importProgress);
    panel.addView(actions);
    return panel;
  }

  void renderImport() {
    if (importPanel == null || isDestroyed()) return;
    if (imports.completed != null) {
      imported = imports.completed;
      imports.completed = null;
      if (tab == LIBRARY) showTab(LIBRARY);
    }
    importPanel.setVisibility(imports.busy || !imports.message.isEmpty() ? View.VISIBLE : View.GONE);
    importHeading.setText(
        imports.busy
            ? (imports.cancelled ? "Cancelling…" : "Adding audiobook")
            : imports.message);
    long idle = (System.currentTimeMillis() - imports.changed) / 1000;
    String amount = String.format(Locale.US, "%.1f MB", imports.copied / 1048576.0);
    if (imports.total > 0)
      amount +=
          String.format(
              Locale.US,
              " of %.1f MB · %d%%",
              imports.total / 1048576.0,
              Math.min(100, imports.copied * 100 / imports.total));
    importDetail.setText(
        imports.filename
            + "\n"
            + (imports.copied == 0 ? "Waiting for the file…" : amount)
            + (idle > 20 && imports.busy
                ? "\nStill waiting. Check your connection, or cancel and try again."
                : ""));
    importDetail.setVisibility(imports.busy ? View.VISIBLE : View.GONE);
    importProgress.setVisibility(imports.busy ? View.VISIBLE : View.GONE);
    importProgress.setIndeterminate(imports.total <= 0 || imports.copied == 0);
    importProgress.setProgress(imports.total > 0 ? (int) (imports.copied * 100 / imports.total) : 0);
    importAction.setText(imports.busy ? "Cancel" : "Dismiss");
    importAction.setEnabled(!imports.cancelled || !imports.busy);
    importPlay.setVisibility(!imports.busy && imported != null ? View.VISIBLE : View.GONE);
  }

  void attachText(Uri uri) {
    File book = textTarget;
    if (book == null) return;
    io.execute(
        () -> {
          File temp = null;
          try {
            temp = File.createTempFile("captions-", ".vtt", getFilesDir());
            try (InputStream in = getContentResolver().openInputStream(uri);
                OutputStream out = new FileOutputStream(temp)) {
              if (in == null) throw new IOException("Cannot open transcript");
              byte[] buffer = new byte[8192];
              int n, total = 0;
              while ((n = in.read(buffer)) != -1) {
                total += n;
                if (total > 16 * 1024 * 1024) throw new IOException("Transcript exceeds 16 MB");
                out.write(buffer, 0, n);
              }
            }
            Transcript.vtt(temp);
            java.nio.file.Files.move(
                temp.toPath(),
                new File(book.getAbsolutePath() + ".vtt").toPath(),
                java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            runOnUiThread(
                () -> {
                  if (book.equals(current())) bookChanged();
                });
          } catch (Exception e) {
            if (temp != null) temp.delete();
            runOnUiThread(() -> error("Could not attach text", e.getMessage()));
          }
        });
  }

  // -- shell: mini player and navigation ----------------------------------------------------------

  LinearLayout buildMiniPlayer() {
    LinearLayout bar = row();
    bar.setPadding(dp(8), dp(8), dp(6), dp(8));
    bar.setBackground(ripple(shape(RAISED, 16)));
    miniCover = new FrameLayout(this);
    bar.addView(miniCover, new LinearLayout.LayoutParams(dp(40), dp(40)));
    LinearLayout details = column();
    details.setPadding(dp(12), 0, dp(8), 0);
    bar.addView(details, new LinearLayout.LayoutParams(0, -2, 1));
    miniTitle = rowLine("", 14, TEXT);
    miniTitle.setTypeface(null, Typeface.BOLD);
    miniDetail = rowLine("", 12, MUTED);
    miniProgress = progressLine(2);
    miniProgress.setMax(1000);
    details.addView(miniTitle);
    details.addView(miniDetail);
    LinearLayout.LayoutParams pp = new LinearLayout.LayoutParams(-1, dp(2));
    pp.topMargin = dp(6);
    details.addView(miniProgress, pp);
    miniPlay = iconButton(R.drawable.ic_play, "Play", this::togglePlay, 44, false);
    bar.addView(miniPlay);
    bar.setOnClickListener(v -> showTab(LISTEN));
    bar.setVisibility(View.GONE);
    return bar;
  }

  void renderMini() {
    if (mini == null) return;
    File book = current();
    mini.setVisibility(book != null && tab != LISTEN ? View.VISIBLE : View.GONE);
    if (book == null || tab == LISTEN) return;
    if (!book.equals(miniBook)) {
      miniBook = book;
      miniCover.removeAllViews();
      miniCover.addView(cover(book, 40, 40, 8));
      miniTitle.setText(Books.title(this, book));
      mini.setContentDescription("Open player for " + Books.title(this, book));
    }
    long pos = position(), d = duration();
    int ci = chapterAt(pos);
    miniDetail.setText(
        (ci >= 0 ? chapters.get(ci).title() + "  ·  " : "") + (d > 0 ? span(d - pos) + " left" : clock(pos)));
    miniProgress.setProgress(d > 0 ? (int) (pos * 1000 / d) : 0);
    boolean on = playing();
    miniPlay.setImageResource(on ? R.drawable.ic_pause : R.drawable.ic_play);
    miniPlay.setContentDescription(on ? "Pause" : "Play");
  }

  LinearLayout buildNav() {
    LinearLayout nav = row();
    nav.setBackgroundColor(SURFACE);
    String[] labels = {"Library", "Listen", "Read"};
    int[] icons = {R.drawable.ic_library, R.drawable.ic_listen, R.drawable.ic_read};
    for (int i = 0; i < 3; i++) {
      int target = i;
      LinearLayout item = column();
      item.setGravity(Gravity.CENTER_HORIZONTAL);
      item.setBackground(ripple(null));
      item.setOnClickListener(v -> showTab(target));
      item.setContentDescription(labels[i]);
      navPills[i] = new FrameLayout(this);
      navIcons[i] = new ImageView(this);
      navIcons[i].setImageResource(icons[i]);
      navPills[i].addView(navIcons[i], new FrameLayout.LayoutParams(dp(24), dp(24), Gravity.CENTER));
      item.addView(navPills[i], new LinearLayout.LayoutParams(dp(60), dp(32)));
      navLabels[i] = text(labels[i], 12, MUTED);
      navLabels[i].setGravity(Gravity.CENTER);
      navLabels[i].setPadding(0, dp(4), 0, 0);
      item.addView(navLabels[i]);
      nav.addView(item, new LinearLayout.LayoutParams(0, -2, 1));
    }
    return nav;
  }

  // -- building blocks ----------------------------------------------------------------------------

  int dp(float n) {
    return Math.round(n * getResources().getDisplayMetrics().density);
  }

  LinearLayout column() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.VERTICAL);
    return l;
  }

  LinearLayout row() {
    LinearLayout l = new LinearLayout(this);
    l.setGravity(Gravity.CENTER_VERTICAL);
    return l;
  }

  TextView text(String s, float size, int color) {
    TextView t = new TextView(this);
    t.setText(s);
    t.setTextSize(size);
    t.setTextColor(color);
    return t;
  }

  /** A title or name in a list: lines up with the rest of the row whatever its language. */
  TextView label(String s, float size, int color) {
    TextView t = text(s, size, color);
    t.setTextAlignment(View.TEXT_ALIGNMENT_VIEW_START);
    return t;
  }

  TextView oneLine(String s, float size, int color) {
    TextView t = text(s, size, color);
    t.setSingleLine(true);
    t.setEllipsize(TextUtils.TruncateAt.END);
    return t;
  }

  TextView rowLine(String s, float size, int color) {
    TextView t = oneLine(s, size, color);
    t.setTextAlignment(View.TEXT_ALIGNMENT_VIEW_START);
    return t;
  }

  TextView eyebrow(String s) {
    TextView t = text(s, 12, MUTED);
    t.setLetterSpacing(.12f);
    t.setTypeface(null, Typeface.BOLD);
    t.setPadding(0, dp(24), 0, dp(10));
    return t;
  }

  GradientDrawable shape(int color, int radius) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(radius));
    return d;
  }

  Drawable ripple(Drawable content) {
    return new RippleDrawable(
        ColorStateList.valueOf(0x3378efa3), content, content == null ? null : shape(Color.WHITE, 16));
  }

  Button button(String label, Runnable action, boolean primary) {
    Button b = new Button(this);
    b.setText(label);
    b.setAllCaps(false);
    b.setTextSize(15);
    b.setTypeface(null, Typeface.BOLD);
    b.setTextColor(primary ? BG : TEXT);
    b.setBackground(ripple(shape(primary ? GREEN : RAISED, 26)));
    b.setMinHeight(dp(52));
    b.setPadding(dp(24), dp(8), dp(24), dp(8));
    b.setStateListAnimator(null);
    b.setOnClickListener(v -> action.run());
    return b;
  }

  Button flatButton(String label, Runnable action) {
    Button b = button(label, action, false);
    b.setBackground(ripple(shape(GREEN_DIM, 20)));
    b.setMinHeight(dp(40));
    b.setMinimumHeight(dp(40));
    b.setPadding(dp(14), 0, dp(14), 0);
    return b;
  }

  ImageView iconButton(int icon, String description, Runnable action, int size, boolean primary) {
    ImageView b = new ImageView(this);
    b.setImageResource(icon);
    b.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
    int pad = dp(size * (primary ? .26f : .24f));
    b.setPadding(pad, pad, pad, pad);
    b.setImageTintList(ColorStateList.valueOf(primary ? BG : TEXT));
    GradientDrawable circle = new GradientDrawable();
    circle.setShape(GradientDrawable.OVAL);
    circle.setColor(primary ? GREEN : Color.TRANSPARENT);
    b.setBackground(new RippleDrawable(ColorStateList.valueOf(0x3378efa3), circle, oval()));
    b.setContentDescription(description);
    b.setClickable(true);
    if (action != null) b.setOnClickListener(v -> action.run());
    b.setLayoutParams(new LinearLayout.LayoutParams(dp(size), dp(size)));
    return b;
  }

  GradientDrawable oval() {
    GradientDrawable d = new GradientDrawable();
    d.setShape(GradientDrawable.OVAL);
    d.setColor(Color.WHITE);
    return d;
  }

  /** A skip button: the round arrow with its seconds written inside. */
  View skipButton(int icon, String seconds, String description, Runnable action) {
    FrameLayout frame = new FrameLayout(this);
    ImageView arrow = new ImageView(this);
    arrow.setImageResource(icon);
    arrow.setImageTintList(ColorStateList.valueOf(TEXT));
    frame.addView(arrow, new FrameLayout.LayoutParams(dp(36), dp(36), Gravity.CENTER));
    TextView label = text(seconds, 10, TEXT);
    label.setTypeface(null, Typeface.BOLD);
    FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER);
    lp.topMargin = dp(3);
    frame.addView(label, lp);
    frame.setBackground(new RippleDrawable(ColorStateList.valueOf(0x3378efa3), null, oval()));
    frame.setContentDescription(description);
    frame.setOnClickListener(v -> action.run());
    frame.setLayoutParams(new LinearLayout.LayoutParams(dp(60), dp(60)));
    return frame;
  }

  TextView chip(int icon, String label, Runnable action) {
    TextView t = text(label, 13, TEXT);
    t.setGravity(Gravity.CENTER);
    t.setSingleLine(true);
    t.setEllipsize(TextUtils.TruncateAt.END);
    Drawable d = getDrawable(icon).mutate();
    d.setBounds(0, 0, dp(18), dp(18));
    t.setCompoundDrawablesRelative(d, null, null, null);
    t.setCompoundDrawablePadding(dp(6));
    t.setCompoundDrawableTintList(ColorStateList.valueOf(MUTED));
    t.setPadding(dp(10), 0, dp(10), 0);
    t.setBackground(ripple(shape(RAISED, 20)));
    if (action != null) t.setOnClickListener(v -> action.run());
    return t;
  }

  ProgressBar progressLine(int height) {
    ProgressBar bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
    bar.setProgressDrawable(track(height));
    return bar;
  }

  LayerDrawable track(int height) {
    ClipDrawable fill = new ClipDrawable(shape(GREEN, height), Gravity.START, ClipDrawable.HORIZONTAL);
    LayerDrawable layers = new LayerDrawable(new Drawable[] {shape(TRACK, height), fill});
    layers.setId(0, android.R.id.background);
    layers.setId(1, android.R.id.progress);
    for (int i = 0; i < 2; i++) {
      layers.setLayerHeight(i, dp(height));
      layers.setLayerGravity(i, Gravity.CENTER_VERTICAL | Gravity.FILL_HORIZONTAL);
    }
    return layers;
  }

  /** A thick, easy to grab progress bar. */
  void styleSeekBar(SeekBar s) {
    s.setProgressDrawable(track(6));
    GradientDrawable thumb = oval();
    thumb.setColor(GREEN);
    thumb.setSize(dp(22), dp(22));
    s.setThumb(thumb);
    s.setThumbOffset(dp(11));
    s.setPadding(dp(16), 0, dp(16), 0);
    s.setSplitTrack(false);
  }

  View emptyState(int icon, String title, String message, String action, Runnable run) {
    LinearLayout box = column();
    box.setGravity(Gravity.CENTER);
    box.setPadding(dp(32), dp(48), dp(32), dp(48));
    ImageView art = new ImageView(this);
    art.setImageResource(icon);
    if (icon != R.drawable.ic_book) {
      art.setImageTintList(ColorStateList.valueOf(GREEN));
      art.setPadding(dp(24), dp(24), dp(24), dp(24));
      GradientDrawable circle = oval();
      circle.setColor(GREEN_DIM);
      art.setBackground(circle);
    }
    box.addView(art, new LinearLayout.LayoutParams(dp(104), dp(104)));
    TextView heading = text(title, 22, TEXT);
    heading.setTypeface(Typeface.create("serif", Typeface.BOLD));
    heading.setGravity(Gravity.CENTER);
    heading.setPadding(0, dp(20), 0, dp(8));
    box.addView(heading);
    TextView note = text(message, 15, MUTED);
    note.setGravity(Gravity.CENTER);
    note.setLineSpacing(dp(2), 1);
    box.addView(note);
    LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(-2, -2);
    bp.topMargin = dp(24);
    box.addView(button(action, run, true), bp);
    return box;
  }

  View cover(File f, int width, int height, int radius) {
    FrameLayout frame = new FrameLayout(this);
    frame.setLayoutParams(new LinearLayout.LayoutParams(dp(width), dp(height)));
    GradientDrawable bg =
        new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[] {0xff375f48, 0xff182b23});
    bg.setCornerRadius(dp(radius));
    frame.setBackground(bg);
    frame.setClipToOutline(true);
    String title = Books.title(this, f);
    TextView label =
        text(width < 48 ? title.substring(0, Math.min(1, title.length())) : title, width >= 120 ? 20 : width >= 70 ? 12 : width >= 48 ? 10 : 16, 0xffe9efdf);
    label.setTypeface(Typeface.create("serif", Typeface.BOLD));
    label.setGravity(Gravity.CENTER);
    label.setPadding(dp(4), dp(4), dp(4), dp(4));
    label.setMaxLines(width >= 70 ? 5 : 4);
    label.setEllipsize(TextUtils.TruncateAt.END);
    frame.addView(label, new FrameLayout.LayoutParams(-1, -1));
    int size = dp(Math.max(width, height));
    Bitmap cached = Books.covers.get(f.getAbsolutePath() + "@" + size);
    if (cached != null) showCover(frame, cached);
    else
      io.execute(
          () -> {
            Bitmap bitmap = Books.cover(f, size);
            if (bitmap != null)
              runOnUiThread(
                  () -> {
                    if (!isDestroyed()) showCover(frame, bitmap);
                  });
          });
    return frame;
  }

  void showCover(FrameLayout frame, Bitmap bitmap) {
    ImageView image = new ImageView(this);
    image.setImageBitmap(bitmap);
    image.setScaleType(ImageView.ScaleType.CENTER_CROP);
    frame.removeAllViews();
    frame.addView(image, new FrameLayout.LayoutParams(-1, -1));
  }

  /** The player's cover, as tall as the picture's own proportions allow. */
  View bigCover(File f, int width) {
    View frame = cover(f, width, (int) (width * 1.4f), 14);
    frame.setElevation(dp(10));
    io.execute(
        () -> {
          Bitmap bitmap = Books.cover(f, dp(width * 1.4f));
          if (bitmap == null) return;
          runOnUiThread(
              () -> {
                float ratio = Math.max(.8f, Math.min(1.4f, bitmap.getHeight() / (float) bitmap.getWidth()));
                ViewGroup.LayoutParams lp = frame.getLayoutParams();
                lp.height = dp(width * ratio);
                frame.setLayoutParams(lp);
              });
        });
    return frame;
  }

  static String clock(long ms) {
    long s = Math.max(0, ms / 1000);
    return s >= 3600
        ? String.format(Locale.US, "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
        : String.format(Locale.US, "%d:%02d", s / 60, s % 60);
  }

  /** A rough length: "16h 4m", "42m". */
  static String span(long ms) {
    long minutes = Math.max(ms > 0 ? 1 : 0, ms / 60000);
    return minutes >= 60 ? minutes / 60 + "h " + minutes % 60 + "m" : minutes + "m";
  }

  static String speedLabel(float speed) {
    String s = String.format(Locale.US, "%.2f", speed).replaceAll("0+$", "").replaceAll("\\.$", "");
    return s + "×";
  }

  void error(String title, String message) {
    if (!isDestroyed())
      new AlertDialog.Builder(this)
          .setTitle(title)
          .setMessage(message)
          .setPositiveButton("OK", null)
          .show();
  }
}
