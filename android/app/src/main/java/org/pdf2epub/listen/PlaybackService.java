package org.pdf2epub.listen;

import android.app.PendingIntent;
import android.content.Intent;
import android.os.Bundle;
import android.view.KeyEvent;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import androidx.media3.common.*;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import androidx.media3.extractor.DefaultExtractorsFactory;
import androidx.media3.session.*;
import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;
import java.io.File;
import java.util.List;

/**
 * Plays the book and remembers the place: the service keeps playing after the app's screens are
 * closed, so it, not the screens, saves the position (every few seconds, on pause and seek, and when
 * the app is swiped away), and reopens the last book where it stopped.
 */
public class PlaybackService extends MediaSessionService {
  /** Sleep timer, set by the screens (same process): when to pause, 0 for never. */
  static volatile long sleepAt;

  /** Pause when the book reaches this position (end of chapter), or -1. */
  static volatile long sleepAtPosition = -1;

  static final SessionCommand BACK = new SessionCommand("back15", Bundle.EMPTY);
  static final SessionCommand FORWARD = new SessionCommand("forward30", Bundle.EMPTY);

  private MediaSession session;
  private ExoPlayer player;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private int ticks;

  @androidx.annotation.OptIn(markerClass = androidx.media3.common.util.UnstableApi.class)
  public void onCreate() {
    super.onCreate();
    // MP3 files without a seek table could not be skipped through at all.
    DefaultExtractorsFactory extractors =
        new DefaultExtractorsFactory().setConstantBitrateSeekingEnabled(true);
    player =
        new ExoPlayer.Builder(this, new DefaultMediaSourceFactory(this, extractors))
            .setSeekBackIncrementMs(15000)
            .setSeekForwardIncrementMs(30000)
            .setAudioAttributes(
                new AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH)
                    .build(),
                true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_LOCAL)
            .build();
    player.addListener(
        new Player.Listener() {
          public void onIsPlayingChanged(boolean playing) {
            File book = book();
            if (playing && book != null) Books.played(PlaybackService.this, book);
            save();
          }

          public void onPositionDiscontinuity(
              Player.PositionInfo old, Player.PositionInfo now, int reason) {
            save();
          }

          public void onPlaybackParametersChanged(PlaybackParameters parameters) {
            save();
          }

          public void onMediaItemTransition(MediaItem item, int reason) {
            File book = book();
            if (book != null) Books.setLast(PlaybackService.this, book);
          }

          public void onPlaybackStateChanged(int state) {
            File book = book();
            if (book == null) return;
            if (state == Player.STATE_READY)
              Books.saveDuration(PlaybackService.this, book, player.getDuration());
            if (state == Player.STATE_ENDED) Books.finish(PlaybackService.this, book);
          }
        });
    File last = Books.last(this);
    if (last != null) {
      // Ready to resume, but not prepared: the notification appears only once the listener plays.
      player.setMediaItem(Books.item(this, last), Books.position(this, last));
      player.setPlaybackSpeed(Books.speed(this, last));
    }
    session =
        new MediaSession.Builder(this, player)
            // The notification and lock screen skip 15 s back and 30 s on, not to the previous track.
            .setMediaButtonPreferences(
                List.of(
                    new CommandButton.Builder(CommandButton.ICON_SKIP_BACK_15)
                        .setSessionCommand(BACK)
                        .setDisplayName("Back 15 seconds")
                        .setSlots(CommandButton.SLOT_BACK)
                        .build(),
                    new CommandButton.Builder(CommandButton.ICON_SKIP_FORWARD_30)
                        .setSessionCommand(FORWARD)
                        .setDisplayName("Forward 30 seconds")
                        .setSlots(CommandButton.SLOT_FORWARD)
                        .build()))
            .setSessionActivity(
                PendingIntent.getActivity(
                    this,
                    0,
                    new Intent(this, MainActivity.class),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT))
            .setCallback(
                new MediaSession.Callback() {
                  // A book is one long track: Android's media controls show "previous/next track"
                  // whenever those commands exist, so they are replaced by the skip buttons.
                  public MediaSession.ConnectionResult onConnect(
                      MediaSession session, MediaSession.ControllerInfo controller) {
                    return new MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                        .setAvailableSessionCommands(
                            MediaSession.ConnectionResult.DEFAULT_SESSION_COMMANDS
                                .buildUpon()
                                .add(BACK)
                                .add(FORWARD)
                                .build())
                        .setAvailablePlayerCommands(
                            MediaSession.ConnectionResult.DEFAULT_PLAYER_COMMANDS
                                .buildUpon()
                                .removeAll(
                                    Player.COMMAND_SEEK_TO_PREVIOUS,
                                    Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
                                    Player.COMMAND_SEEK_TO_NEXT,
                                    Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM)
                                .build())
                        .build();
                  }

                  public ListenableFuture<SessionResult> onCustomCommand(
                      MediaSession session,
                      MediaSession.ControllerInfo controller,
                      SessionCommand command,
                      Bundle args) {
                    if (command.equals(BACK)) player.seekBack();
                    else if (command.equals(FORWARD)) player.seekForward();
                    return Futures.immediateFuture(new SessionResult(SessionResult.RESULT_SUCCESS));
                  }

                  // Headset and car buttons: next/previous skip within the book.
                  public boolean onMediaButtonEvent(
                      MediaSession session, MediaSession.ControllerInfo controller, Intent intent) {
                    KeyEvent key = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
                    if (key == null || key.getAction() != KeyEvent.ACTION_DOWN) return false;
                    switch (key.getKeyCode()) {
                      case KeyEvent.KEYCODE_MEDIA_NEXT, KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> {
                        player.seekForward();
                        return true;
                      }
                      case KeyEvent.KEYCODE_MEDIA_PREVIOUS, KeyEvent.KEYCODE_MEDIA_REWIND -> {
                        player.seekBack();
                        return true;
                      }
                      default -> {
                        return false;
                      }
                    }
                  }

                  // Play from a headset or the system's media controls after the app was closed.
                  public ListenableFuture<MediaSession.MediaItemsWithStartPosition>
                      onPlaybackResumption(
                          MediaSession session, MediaSession.ControllerInfo controller) {
                    File book = Books.last(PlaybackService.this);
                    if (book == null)
                      return Futures.immediateFailedFuture(new IllegalStateException("No book"));
                    return Futures.immediateFuture(
                        new MediaSession.MediaItemsWithStartPosition(
                            List.of(Books.item(PlaybackService.this, book)),
                            0,
                            Books.position(PlaybackService.this, book)));
                  }
                })
            .build();
    handler.post(this::tick);
  }

  private File book() {
    MediaItem item = player.getCurrentMediaItem();
    return item == null || item.mediaId.isEmpty() ? null : new File(item.mediaId);
  }

  private void save() {
    File book = book();
    if (book == null || player.getPlaybackState() == Player.STATE_ENDED) return;
    Books.save(this, book, player.getCurrentPosition(), player.getPlaybackParameters().speed);
  }

  private void tick() {
    long left = sleepAt == 0 ? Long.MAX_VALUE : sleepAt - SystemClock.elapsedRealtime();
    if (left <= 0 || (sleepAtPosition >= 0 && player.getCurrentPosition() >= sleepAtPosition)) {
      sleepAt = 0;
      sleepAtPosition = -1;
      if (player.isPlaying()) player.pause();
      left = Long.MAX_VALUE;
    }
    float volume = left < 10000 ? left / 10000f : 1; // the timer fades out over its last seconds
    if (player.getVolume() != volume) player.setVolume(volume);
    if (player.isPlaying() && ++ticks % 10 == 0) save();
    handler.postDelayed(this::tick, 500);
  }

  public MediaSession onGetSession(MediaSession.ControllerInfo info) {
    return session;
  }

  public void onTaskRemoved(Intent intent) {
    save();
    super.onTaskRemoved(intent);
  }

  public void onDestroy() {
    save();
    handler.removeCallbacksAndMessages(null);
    player.release();
    session.release();
    super.onDestroy();
  }
}
