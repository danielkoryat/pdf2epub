import { File } from 'expo-file-system';
import { router } from 'expo-router';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, FlatList, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Button, EmptyState, Icon } from '../components/ui';
import { attachText, setSettings, useLibrary } from '../lib/library';
import { type Cue, readCues } from '../lib/mp4';
import { open, seekTo, usePlayback } from '../lib/player';
import { colors, serif } from '../lib/theme';

const SIZES = [16, 19, 22, 26];
const NO_TEXT =
  'This audiobook has no read-along text. Books made by the pdf2epub site include it. For another book, attach a matching WebVTT (.vtt) transcript.';

/** The passage being read at `seconds`; between passages, the one just read. */
function cueAt(cues: Cue[], seconds: number) {
  let lo = 0;
  let hi = cues.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cues[mid].start <= seconds) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

export default function Read() {
  const book = usePlayback((p) => p.book);
  const playing = usePlayback((p) => p.playing);
  const size = useLibrary((s) => s.settings.readSize);
  // A new books array means a transcript may have been attached: read the text again.
  const books = useLibrary((s) => s.books);
  const [loaded, setLoaded] = useState<{ id: string; cues: Cue[]; error: string } | null>(null);
  const list = useRef<FlatList<Cue>>(null);
  const [follow, setFollow] = useState(true);

  useEffect(() => {
    if (!book) return;
    const timer = setTimeout(() => {
      try {
        setLoaded({ id: book.id, cues: readCues(new File(book.uri), book.text), error: '' });
      } catch (error) {
        setLoaded({ id: book.id, cues: [], error: `Could not read this book's text: ${(error as Error).message}` });
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [book, books]);

  const cues = useMemo(() => (loaded && loaded.id === book?.id ? loaded.cues : []), [loaded, book]);
  // Only the passage index, not every position update, re-renders the list.
  const index = usePlayback((p) => cueAt(cues, p.position));

  useEffect(() => {
    if (follow && index >= 0 && cues.length > 0)
      list.current?.scrollToIndex({ index, viewPosition: 0.2, animated: true });
  }, [index, follow, cues]);

  if (!book)
    return (
      <EmptyState
        icon="book-open-variant"
        title="Nothing to read yet"
        message="Start a book from your library, then follow its text here while you listen."
        action="Open library"
        onAction={() => router.navigate('/')}
      />
    );

  if (!loaded || loaded.id !== book.id) return null;
  if (cues.length === 0)
    return (
      <ScrollView contentContainerStyle={{ padding: 24 }}>
        <Text style={styles.heading}>Read along</Text>
        <Text style={styles.note}>{loaded.error || NO_TEXT}</Text>
        <Button
          label="Attach timed text (.vtt)"
          onPress={() => attachText(book.id).catch((e: Error) => Alert.alert('Could not attach text', e.message))}
        />
      </ScrollView>
    );

  const nextSize = SIZES[(SIZES.indexOf(size) + 1) % SIZES.length] ?? 19;
  return (
    <View style={{ flex: 1 }}>
      <View style={styles.header}>
        <Text numberOfLines={1} style={styles.bookTitle}>
          {book.title}
        </Text>
        <Pressable onPress={() => setSettings({ readSize: nextSize })} style={styles.size} accessibilityLabel="Text size">
          <Text style={{ color: colors.text, fontSize: 17 }}>Aa</Text>
        </Pressable>
      </View>
      <FlatList
        ref={list}
        data={cues}
        keyExtractor={(_, i) => String(i)}
        extraData={`${index}:${size}`}
        onScrollToIndexFailed={(info) => {
          // Passages far away are not measured yet: jump close by estimate, then exactly.
          list.current?.scrollToOffset({ offset: info.averageItemLength * info.index, animated: false });
          setTimeout(() => list.current?.scrollToIndex({ index: info.index, viewPosition: 0.2, animated: false }), 100);
        }}
        onScrollBeginDrag={() => setFollow(false)}
        contentContainerStyle={{ paddingHorizontal: 12, paddingTop: 8, paddingBottom: 96 }}
        windowSize={11}
        renderItem={({ item, index: i }) => (
          <Passage
            text={item.text}
            state={i === index ? 'now' : i < index ? 'read' : 'next'}
            size={size}
            onPress={() => {
              setFollow(true);
              seekTo(item.start);
              if (!playing) open(book);
            }}
          />
        )}
      />
      {!follow && (
        <Pressable onPress={() => setFollow(true)} style={styles.follow} accessibilityLabel="Back to the passage being read">
          <Icon name="crosshairs-gps" size={18} color={colors.bg} />
          <Text style={styles.followText}>Back to now</Text>
        </Pressable>
      )}
    </View>
  );
}

const Passage = memo(function Passage({
  text,
  state,
  size,
  onPress,
}: {
  text: string;
  state: 'now' | 'read' | 'next';
  size: number;
  onPress: () => void;
}) {
  return (
    <Pressable onPress={onPress} style={[styles.passage, state === 'now' && styles.now]}>
      <Text
        style={[
          styles.passageText,
          { fontSize: size, lineHeight: size * 1.45, color: state === 'now' ? colors.text : state === 'read' ? colors.faint : colors.upcoming },
        ]}>
        {text}
      </Text>
    </Pressable>
  );
});

const styles = StyleSheet.create({
  heading: { color: colors.text, fontFamily: serif, fontWeight: '700', fontSize: 26 },
  note: { color: colors.muted, fontSize: 16, lineHeight: 23, marginTop: 10, marginBottom: 20 },
  header: { flexDirection: 'row', alignItems: 'center', paddingLeft: 20, paddingRight: 8, paddingTop: 8, paddingBottom: 4 },
  bookTitle: { flex: 1, color: colors.muted, fontSize: 15, textAlign: 'left', writingDirection: 'auto', marginRight: 12 },
  size: { width: 52, height: 40, borderRadius: 20, backgroundColor: colors.raised, alignItems: 'center', justifyContent: 'center' },
  passage: { paddingHorizontal: 14, paddingVertical: 10, borderRadius: 14 },
  now: { backgroundColor: colors.greenDim },
  passageText: { fontFamily: serif, writingDirection: 'auto' },
  follow: {
    position: 'absolute',
    bottom: 16,
    alignSelf: 'center',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingHorizontal: 18,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.green,
  },
  followText: { color: colors.bg, fontSize: 14, fontWeight: '700' },
});
