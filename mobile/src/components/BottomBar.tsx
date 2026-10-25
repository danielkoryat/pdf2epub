import type { BottomTabBarProps } from 'expo-router/tabs';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { chapterAt, toggle, usePlayback } from '../lib/player';
import { clock, colors, span } from '../lib/theme';
import { Cover, Icon, IconButton, type IconName, ProgressLine } from './ui';

const TABS: Record<string, { label: string; icon: IconName }> = {
  index: { label: 'Library', icon: 'bookshelf' },
  listen: { label: 'Listen', icon: 'headphones' },
  read: { label: 'Read', icon: 'book-open-variant' },
};

/** The tabs, with a small player above them on the screens that are not the player. */
export function BottomBar({ state, navigateToTab }: BottomTabBarProps) {
  const insets = useSafeAreaInsets();
  const current = state.routes[state.index].name;
  return (
    <View>
      {current !== 'listen' && <MiniPlayer onOpen={() => navigateToTab(state.routes.find((r) => r.name === 'listen')!.key)} />}
      <View style={[styles.nav, { paddingBottom: insets.bottom + 8 }]}>
        {state.routes.map((route, i) => {
          const tab = TABS[route.name];
          if (!tab) return null;
          const on = i === state.index;
          return (
            <Pressable
              key={route.key}
              accessibilityRole="tab"
              accessibilityState={{ selected: on }}
              accessibilityLabel={tab.label}
              onPress={() => navigateToTab(route.key)}
              style={styles.tab}>
              <View style={[styles.pill, on && { backgroundColor: colors.greenDim }]}>
                <Icon name={tab.icon} size={24} color={on ? colors.green : colors.muted} />
              </View>
              <Text style={[styles.label, on && { color: colors.text, fontWeight: '700' }]}>{tab.label}</Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

function MiniPlayer({ onOpen }: { onOpen: () => void }) {
  const book = usePlayback((p) => p.book);
  const position = usePlayback((p) => p.position);
  const duration = usePlayback((p) => p.duration);
  const playing = usePlayback((p) => p.playing);
  if (!book) return null;
  const chapter = book.chapters[chapterAt(book, position)];
  const time = duration > 0 ? `${span(duration - position)} left` : clock(position);
  return (
    <Pressable onPress={onOpen} accessibilityLabel={`Open player for ${book.title}`} style={styles.mini}>
      <Cover book={book} width={40} height={40} radius={8} />
      <View style={{ flex: 1, marginHorizontal: 12 }}>
        <Text numberOfLines={1} style={styles.miniTitle}>
          {book.title}
        </Text>
        <Text numberOfLines={1} style={styles.miniDetail}>
          {chapter ? `${chapter.title}  ·  ${time}` : time}
        </Text>
        <ProgressLine value={duration > 0 ? position / duration : 0} height={2} style={{ marginTop: 6 }} />
      </View>
      <IconButton icon={playing ? 'pause' : 'play'} label={playing ? 'Pause' : 'Play'} onPress={toggle} size={44} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  nav: { flexDirection: 'row', backgroundColor: colors.surface, paddingTop: 2 },
  tab: { flex: 1, alignItems: 'center', paddingTop: 6, paddingBottom: 2 },
  pill: { width: 60, height: 32, borderRadius: 16, alignItems: 'center', justifyContent: 'center' },
  label: { color: colors.muted, fontSize: 12, marginTop: 4 },
  mini: {
    flexDirection: 'row',
    alignItems: 'center',
    marginHorizontal: 10,
    marginBottom: 6,
    padding: 8,
    paddingRight: 6,
    borderRadius: 16,
    backgroundColor: colors.raised,
  },
  miniTitle: { color: colors.text, fontSize: 14, fontWeight: '700', textAlign: 'left', writingDirection: 'auto' },
  miniDetail: { color: colors.muted, fontSize: 12, marginTop: 2, textAlign: 'left' },
});
