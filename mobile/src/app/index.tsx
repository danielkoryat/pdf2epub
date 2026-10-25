import { router } from 'expo-router';
import { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Sheet } from '../components/Sheet';
import { Cover, EmptyState, Eyebrow, IconButton, ProgressLine } from '../components/ui';
import { type Book, deleteBook, importBook, type Progress, sorted, useLibrary } from '../lib/library';
import { close, open, toggle, usePlayback } from '../lib/player';
import { colors, serif, span } from '../lib/theme';

export default function Library() {
  const books = useLibrary((s) => s.books);
  const progress = useLibrary((s) => s.progress);
  const last = useLibrary((s) => s.last);
  const ready = useLibrary((s) => s.ready);
  const current = usePlayback((p) => p.book);
  const [menu, setMenu] = useState<Book | null>(null);

  if (!ready) return null;
  const list = sorted(books, progress);
  const resume = current ?? books.find((b) => b.id === last) ?? null;
  const play = (book: Book, fromStart = false) => {
    open(book, { fromStart });
    router.navigate('/listen');
  };
  const confirmDelete = (book: Book) =>
    Alert.alert(
      'Delete this audiobook?',
      // Starts in English, so a Hebrew title does not turn the whole sentence right to left.
      `This removes "${book.title}" and your place in it from this phone. The original file in Google Drive or your files stays where it is.`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Delete',
          style: 'destructive',
          onPress: () => {
            close(book.id);
            deleteBook(book.id);
          },
        },
      ],
    );

  return (
    <ScrollView contentContainerStyle={styles.page}>
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.title}>Library</Text>
          <Text style={styles.count}>
            {books.length === 0 ? 'No books yet' : `${books.length} ${books.length === 1 ? 'audiobook' : 'audiobooks'}`}
          </Text>
        </View>
        {books.length > 0 && <IconButton icon="plus" label="Add audiobook" onPress={importBook} primary size={48} />}
      </View>

      {books.length === 0 ? (
        <EmptyState
          icon="book-open-page-variant"
          title="Your library is empty"
          message="Add an M4B, M4A or MP3 audiobook from Google Drive or your phone. It is saved here, so you can listen offline."
          action="Add audiobook"
          onAction={importBook}
        />
      ) : (
        <>
          {resume && (
            <>
              <Eyebrow>CONTINUE LISTENING</Eyebrow>
              <ContinueCard book={resume} onOpen={() => play(resume)} />
            </>
          )}
          <Eyebrow>ALL BOOKS</Eyebrow>
          {list.map((book) => (
            <BookRow
              key={book.id}
              book={book}
              progress={progress[book.id]}
              onPlay={() => play(book)}
              onMenu={() => setMenu(book)}
            />
          ))}
        </>
      )}

      <Sheet
        visible={menu !== null}
        title={menu?.title ?? ''}
        onClose={() => setMenu(null)}
        options={
          menu
            ? [
                { label: 'Play', onPress: () => play(menu) },
                ...(progress[menu.id]?.position || progress[menu.id]?.done
                  ? [{ label: 'Play from the beginning', onPress: () => play(menu, true) }]
                  : []),
                { label: 'Delete from library', destructive: true, onPress: () => confirmDelete(menu) },
              ]
            : []
        }
      />
    </ScrollView>
  );
}

function ContinueCard({ book, onOpen }: { book: Book; onOpen: () => void }) {
  const isOpen = usePlayback((p) => p.book?.id === book.id);
  const playing = usePlayback((p) => p.playing && p.book?.id === book.id);
  const live = usePlayback((p) => (p.book?.id === book.id ? p.position : null));
  const saved = useLibrary((s) => s.progress[book.id]?.position ?? 0);
  const position = live ?? saved;
  const duration = book.duration;
  return (
    <Pressable onPress={isOpen ? () => router.navigate('/listen') : onOpen} style={styles.card} accessibilityLabel={`Continue ${book.title}`}>
      <Cover book={book} width={76} height={108} radius={10} />
      <View style={{ flex: 1, marginHorizontal: 14 }}>
        <Text numberOfLines={2} style={styles.cardTitle}>
          {book.title}
        </Text>
        {book.author ? (
          <Text numberOfLines={1} style={styles.author}>
            {book.author}
          </Text>
        ) : null}
        <ProgressLine value={duration > 0 ? position / duration : 0} style={{ marginTop: 12, marginBottom: 6 }} />
        <Text style={styles.status}>
          {duration > 0 ? `${span(duration - position)} left  ·  ${Math.floor((position * 100) / duration)}%` : 'Ready to continue'}
        </Text>
      </View>
      <IconButton
        icon={playing ? 'pause' : 'play'}
        label={playing ? 'Pause' : 'Play'}
        primary
        size={56}
        onPress={isOpen ? toggle : onOpen}
      />
    </Pressable>
  );
}

function BookRow({ book, progress, onPlay, onMenu }: { book: Book; progress?: Progress; onPlay: () => void; onMenu: () => void }) {
  const position = progress?.position ?? 0;
  const d = book.duration;
  const finished = progress?.done || (d > 0 && position > 0 && d - position < 2);
  const begun = !finished && (position > 0 || (progress?.played ?? 0) > 0);
  const status = finished
    ? 'Finished'
    : begun && d > 0
      ? `${Math.floor((position * 100) / d)}%  ·  ${span(d - position)} left`
      : `${d > 0 ? `${span(d)}  ·  ` : ''}New`;
  return (
    <Pressable onPress={onPlay} onLongPress={onMenu} style={styles.row} android_ripple={{ color: '#78EFA322' }} accessibilityLabel={`Play ${book.title}`}>
      <Cover book={book} width={52} height={74} />
      <View style={{ flex: 1, marginHorizontal: 14 }}>
        <Text numberOfLines={2} style={styles.rowTitle}>
          {book.title}
        </Text>
        {book.author ? (
          <Text numberOfLines={1} style={styles.author}>
            {book.author}
          </Text>
        ) : null}
        {begun && d > 0 && <ProgressLine value={position / d} height={3} style={{ width: 120, marginTop: 8 }} />}
        <Text style={[styles.status, { color: begun ? colors.green : colors.faint, marginTop: 4 }]}>{status}</Text>
      </View>
      <IconButton icon="dots-vertical" label={`More for ${book.title}`} onPress={onMenu} size={44} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  page: { flexGrow: 1, paddingHorizontal: 20, paddingTop: 12, paddingBottom: 24 },
  header: { flexDirection: 'row', alignItems: 'center' },
  title: { color: colors.text, fontFamily: serif, fontWeight: '700', fontSize: 30 },
  count: { color: colors.muted, fontSize: 14, marginTop: 2 },
  card: { flexDirection: 'row', alignItems: 'center', padding: 14, borderRadius: 20, backgroundColor: colors.card },
  cardTitle: { color: colors.text, fontFamily: serif, fontWeight: '700', fontSize: 18, textAlign: 'left', writingDirection: 'auto' },
  rowTitle: { color: colors.text, fontSize: 16, textAlign: 'left', writingDirection: 'auto' },
  author: { color: colors.muted, fontSize: 13, marginTop: 2, textAlign: 'left' },
  status: { color: colors.muted, fontSize: 12 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 10, paddingLeft: 8, borderRadius: 14 },
});
