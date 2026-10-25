import { router } from 'expo-router';
import { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { GoToTime } from '../components/GoToTime';
import { Scrubber } from '../components/Scrubber';
import { Sheet } from '../components/Sheet';
import { Cover, EmptyState, Icon, IconButton, Tool } from '../components/ui';
import { importBook, setSettings, useLibrary } from '../lib/library';
import { BACK, chapterAt, FORWARD, seekTo, setSleep, setSpeed, skip, SPEEDS, toggle, usePlayback } from '../lib/player';
import { clock, colors, serif, span, speedLabel } from '../lib/theme';

const SLEEP_MINUTES = [5, 10, 15, 30, 45, 60, 90];

export default function Listen() {
  const book = usePlayback((p) => p.book);
  const position = usePlayback((p) => p.position);
  const duration = usePlayback((p) => p.duration);
  const playing = usePlayback((p) => p.playing);
  const speed = usePlayback((p) => p.speed);
  const sleepAt = usePlayback((p) => p.sleepAt);
  const sleepLeft = usePlayback((p) => p.sleepLeft);
  const sleepAtPosition = usePlayback((p) => p.sleepAtPosition);
  const bookScope = useLibrary((s) => s.settings.bookScope);
  const hasBooks = useLibrary((s) => s.books.length > 0);
  const { width, height } = useWindowDimensions();
  const [dragging, setDragging] = useState<number | null>(null);
  const [sheet, setSheet] = useState<'speed' | 'sleep' | 'chapters' | 'goto' | null>(null);

  if (!book)
    return (
      <EmptyState
        icon="headphones"
        title="Nothing playing"
        message={hasBooks ? 'Choose a book from your library to start listening.' : 'Add an audiobook to start listening.'}
        action={hasBooks ? 'Open library' : 'Add audiobook'}
        onAction={hasBooks ? () => router.navigate('/') : importBook}
      />
    );

  const chapters = book.chapters;
  const ci = chapterAt(book, position);
  // The bar spans the current chapter (precise in a long book), or the whole book.
  const chapterScope = !bookScope && chapters.length > 0;
  const start = chapterScope && ci >= 0 ? chapters[ci].start : 0;
  const end = chapterScope && ci + 1 < chapters.length ? chapters[ci + 1].start : Math.max(duration, 1);
  const coverWidth = Math.min(width - 140, height * 0.26, 260);
  const sleeping = sleepAt !== null || sleepAtPosition !== null;
  const left = Math.max(0, duration - position);

  const previousChapter = () => {
    if (ci < 0) return;
    // Like a music player: back to the start of this chapter, or to the previous one when at its start.
    seekTo(position - chapters[ci].start > 3 || ci === 0 ? chapters[ci].start : chapters[ci - 1].start);
  };
  const nextChapter = () => {
    if (ci + 1 < chapters.length) seekTo(chapters[ci + 1].start);
  };

  return (
    <ScrollView contentContainerStyle={styles.page}>
      <View style={{ alignItems: 'center' }}>
        <Cover book={book} width={coverWidth} height={coverWidth * 1.4} radius={14} />
      </View>
      <Text numberOfLines={2} style={styles.title}>
        {book.title}
      </Text>
      {book.author ? (
        <Text numberOfLines={1} style={styles.author}>
          {book.author}
        </Text>
      ) : null}
      {chapters.length > 0 && (
        <Pressable onPress={() => setSheet('chapters')} style={styles.chapter} accessibilityLabel="Chapters">
          <Text numberOfLines={1} style={styles.chapterText}>
            {ci >= 0 ? chapters[ci].title : 'Opening'}
          </Text>
          <Icon name="chevron-down" size={18} color={colors.green} />
        </Pressable>
      )}

      <View style={{ marginTop: 14 }}>
        <Scrubber
          value={position - start}
          max={end - start}
          onDrag={(v) => setDragging(v === null ? null : start + v)}
          onDone={(v) => seekTo(start + v)}
        />
      </View>
      <View style={styles.times}>
        <Pressable onPress={() => setSheet('goto')} style={{ flex: 1 }}>
          <Text style={[styles.time, dragging !== null && styles.timeDragging]}>
            {dragging !== null ? clock(dragging) : clock(position - start)}
          </Text>
        </Pressable>
        {chapters.length > 0 && (
          <Pressable onPress={() => setSettings({ bookScope: !bookScope })} style={styles.scope} accessibilityLabel={bookScope ? 'The bar shows the whole book. Show this chapter' : 'The bar shows this chapter. Show the whole book'}>
            <Text style={styles.scopeText}>{bookScope ? 'Whole book' : 'Chapter'}</Text>
            <Icon name="swap-horizontal" size={14} color={colors.muted} />
          </Pressable>
        )}
        <Pressable onPress={() => setSheet('goto')} style={{ flex: 1 }}>
          <Text style={[styles.time, { textAlign: 'right' }, dragging !== null && styles.timeDragging]}>
            {dragging !== null
              ? `${dragging < position ? '-' : '+'}${clock(Math.abs(dragging - position))}`
              : `-${clock(end - position)}`}
          </Text>
        </Pressable>
      </View>
      <Text style={styles.where}>
        {chapterScope && ci >= 0
          ? `Chapter ${ci + 1} of ${chapters.length}${duration > 0 ? `  ·  ${span(left)} left in the book` : ''}`
          : duration > 0
            ? `${clock(position)} of ${clock(duration)}  ·  ${span(left)} left`
            : ''}
      </Text>

      <View style={styles.controls}>
        <IconButton icon="skip-previous" label="Previous chapter" onPress={previousChapter} size={52} disabled={chapters.length < 2} />
        <IconButton icon="rewind-15" label={`Back ${BACK} seconds`} onPress={() => skip(-BACK)} size={60} />
        <IconButton icon={playing ? 'pause' : 'play'} label={playing ? 'Pause' : 'Play'} onPress={toggle} size={76} primary />
        <IconButton icon="fast-forward-30" label={`Forward ${FORWARD} seconds`} onPress={() => skip(FORWARD)} size={60} />
        <IconButton icon="skip-next" label="Next chapter" onPress={nextChapter} size={52} disabled={chapters.length < 2} />
      </View>

      <View style={styles.tools}>
        <Tool icon="speedometer" label={speedLabel(speed)} onPress={() => setSheet('speed')} />
        <Tool
          icon="timer-outline"
          label={sleepLeft !== null ? clock(sleepLeft) : sleepAtPosition !== null ? 'Chapter end' : 'Sleep'}
          onPress={() => setSheet('sleep')}
          active={sleeping}
        />
        {chapters.length > 0 && <Tool icon="format-list-bulleted" label="Chapters" onPress={() => setSheet('chapters')} />}
        <Tool icon="clock-outline" label="Go to" onPress={() => setSheet('goto')} />
      </View>

      <Sheet
        visible={sheet === 'speed'}
        title="Playback speed"
        onClose={() => setSheet(null)}
        options={SPEEDS.map((s) => ({ label: speedLabel(s), checked: Math.abs(s - speed) < 0.01, onPress: () => setSpeed(s) }))}
      />
      <Sheet
        visible={sheet === 'sleep'}
        title="Sleep timer"
        onClose={() => setSheet(null)}
        options={[
          { label: 'Off', checked: !sleeping, onPress: () => setSleep(null) },
          ...SLEEP_MINUTES.map((m) => ({
            label: m < 60 || m % 60 ? `${m} minutes` : `${m / 60} hour`,
            onPress: () => setSleep(m),
          })),
          ...(chapters.length > 0
            ? [
                {
                  label: 'End of this chapter',
                  checked: sleepAtPosition !== null,
                  onPress: () => setSleep(null, ci + 1 < chapters.length ? chapters[ci + 1].start - 0.3 : Math.max(0, duration - 0.3)),
                },
              ]
            : []),
        ]}
      />
      <Sheet
        visible={sheet === 'chapters'}
        title="Chapters"
        onClose={() => setSheet(null)}
        options={chapters.map((c, i) => ({ label: c.title, detail: clock(c.start), checked: i === ci, onPress: () => seekTo(c.start) }))}
      />
      <GoToTime visible={sheet === 'goto'} position={position} duration={duration} onClose={() => setSheet(null)} onGo={seekTo} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { paddingHorizontal: 24, paddingTop: 16, paddingBottom: 16 },
  title: { color: colors.text, fontFamily: serif, fontWeight: '700', fontSize: 22, textAlign: 'center', marginTop: 18 },
  author: { color: colors.muted, fontSize: 14, textAlign: 'center', marginTop: 2 },
  chapter: {
    alignSelf: 'center',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    maxWidth: '100%',
    marginTop: 12,
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 18,
    backgroundColor: colors.greenDim,
  },
  chapterText: { color: colors.green, fontSize: 14, flexShrink: 1 },
  times: { flexDirection: 'row', alignItems: 'center' },
  time: { color: colors.muted, fontSize: 13, fontVariant: ['tabular-nums'] },
  timeDragging: { color: colors.green, fontWeight: '700' },
  scope: { flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 12, paddingVertical: 6, borderRadius: 14, backgroundColor: colors.raised },
  scopeText: { color: colors.muted, fontSize: 12 },
  where: { color: colors.faint, fontSize: 12, textAlign: 'center', marginTop: 6 },
  controls: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, marginTop: 14 },
  tools: { flexDirection: 'row', marginTop: 20 },
});
