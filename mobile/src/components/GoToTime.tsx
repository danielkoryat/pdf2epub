import { useState } from 'react';
import { StyleSheet, Text, TextInput, View } from 'react-native';
import { clock, colors } from '../lib/theme';
import { Sheet } from './Sheet';
import { Button } from './ui';

const LABELS = ['hours', 'min', 'sec'];

/** Jump to an exact time: dragging is too coarse in a long book. */
export function GoToTime({
  visible,
  position,
  duration,
  onClose,
  onGo,
}: {
  visible: boolean;
  position: number;
  duration: number;
  onClose: () => void;
  onGo: (seconds: number) => void;
}) {
  return (
    <Sheet visible={visible} title="Go to time" onClose={onClose}>
      {/* Mounted each time the sheet opens, so the fields start at the current position. */}
      {visible && <Fields position={position} duration={duration} onGo={(s) => (onClose(), onGo(s))} />}
    </Sheet>
  );
}

function Fields({ position, duration, onGo }: { position: number; duration: number; onGo: (seconds: number) => void }) {
  const [parts, setParts] = useState(() => {
    const s = Math.floor(position);
    return [String(Math.floor(s / 3600)), String(Math.floor((s / 60) % 60)).padStart(2, '0'), String(s % 60).padStart(2, '0')];
  });
  const go = () => {
    const [h, m, s] = parts.map((p) => Number(p) || 0);
    onGo(h * 3600 + Math.min(m, 59) * 60 + Math.min(s, 59));
  };
  return (
    <View style={{ paddingHorizontal: 24 }}>
      {duration > 0 && <Text style={styles.note}>The book is {clock(duration)} long.</Text>}
      <View style={styles.fields}>
        {parts.map((value, i) => (
          <View key={i} style={{ alignItems: 'center' }}>
            <TextInput
              value={value}
              onChangeText={(text) => setParts(parts.map((p, j) => (j === i ? text.replace(/\D/g, '').slice(0, 3) : p)))}
              keyboardType="number-pad"
              selectTextOnFocus
              accessibilityLabel={LABELS[i]}
              style={styles.field}
            />
            <Text style={styles.label}>{LABELS[i]}</Text>
          </View>
        ))}
      </View>
      <Button label="Go" onPress={go} />
    </View>
  );
}

const styles = StyleSheet.create({
  note: { color: colors.muted, fontSize: 14 },
  fields: { flexDirection: 'row', justifyContent: 'center', gap: 16, marginVertical: 20 },
  field: {
    width: 76,
    height: 60,
    borderRadius: 14,
    backgroundColor: colors.raised,
    color: colors.text,
    fontSize: 26,
    textAlign: 'center',
    fontVariant: ['tabular-nums'],
  },
  label: { color: colors.muted, fontSize: 12, marginTop: 6 },
});
