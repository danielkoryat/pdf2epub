import { router } from 'expo-router';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { cancelImport, dismissImport, getState, useLibrary } from '../lib/library';
import { open } from '../lib/player';
import { colors } from '../lib/theme';
import { ProgressLine } from './ui';

/** Shows a book being copied into the app, and how it went. */
export function ImportBanner() {
  const job = useLibrary((s) => s.import);
  if (!job.busy && !job.message) return null;
  const mb = (bytes: number) => `${(bytes / 1048576).toFixed(1)} MB`;
  const amount =
    job.copied === 0
      ? 'Waiting for the file…'
      : job.total > 0
        ? `${mb(job.copied)} of ${mb(job.total)} · ${Math.min(100, Math.floor((job.copied * 100) / job.total))}%`
        : mb(job.copied);
  const playNow = () => {
    const book = getState().books.find((b) => b.id === job.added);
    dismissImport();
    if (book) {
      open(book);
      router.navigate('/listen');
    }
  };
  return (
    <View style={styles.panel}>
      <View style={{ flexDirection: 'row', alignItems: 'center' }}>
        {job.busy && <ActivityIndicator color={colors.green} style={{ marginRight: 10 }} />}
        <Text style={styles.heading} numberOfLines={2}>
          {job.busy ? 'Adding audiobook' : job.message}
        </Text>
      </View>
      {job.busy && (
        <>
          <Text style={styles.detail} numberOfLines={1}>
            {job.name}
          </Text>
          <Text style={styles.detail}>{amount}</Text>
          <ProgressLine value={job.total > 0 ? job.copied / job.total : 0} style={{ marginTop: 8 }} />
        </>
      )}
      <View style={styles.actions}>
        <Pressable onPress={job.busy ? cancelImport : dismissImport} style={styles.action}>
          <Text style={styles.actionText}>{job.busy ? 'Cancel' : 'Dismiss'}</Text>
        </Pressable>
        {!job.busy && job.added && (
          <Pressable onPress={playNow} style={styles.action}>
            <Text style={[styles.actionText, { color: colors.green }]}>Play now</Text>
          </Pressable>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  panel: { marginHorizontal: 16, marginTop: 8, padding: 16, paddingBottom: 10, borderRadius: 16, backgroundColor: colors.greenDim },
  heading: { flex: 1, color: colors.text, fontSize: 15, fontWeight: '700' },
  detail: { color: colors.muted, fontSize: 13, marginTop: 4 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: 8, marginTop: 8 },
  action: { paddingHorizontal: 14, paddingVertical: 10, borderRadius: 20 },
  actionText: { color: colors.text, fontSize: 15, fontWeight: '700' },
});
