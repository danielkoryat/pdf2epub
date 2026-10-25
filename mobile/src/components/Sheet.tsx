import type { ReactNode } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { colors } from '../lib/theme';
import { Icon } from './ui';

export type Option = { label: string; detail?: string; checked?: boolean; destructive?: boolean; onPress: () => void };

/** A panel that slides up from the bottom: a list of choices, or any content. */
export function Sheet({
  visible,
  title,
  options,
  onClose,
  children,
}: {
  visible: boolean;
  title: string;
  options?: Option[];
  onClose: () => void;
  children?: ReactNode;
}) {
  const insets = useSafeAreaInsets();
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose} statusBarTranslucent navigationBarTranslucent>
      <Pressable style={styles.backdrop} onPress={onClose} accessibilityLabel="Close" />
      <View style={[styles.sheet, { paddingBottom: insets.bottom + 12 }]}>
        <View style={styles.handle} />
        <Text style={styles.title}>{title}</Text>
        {children}
        {options && (
          <ScrollView style={{ maxHeight: 460 }}>
            {options.map((o, i) => (
              <Pressable
                key={i}
                accessibilityRole="button"
                accessibilityState={{ selected: o.checked }}
                onPress={() => {
                  onClose();
                  o.onPress();
                }}
                android_ripple={{ color: '#78EFA322' }}
                style={styles.option}>
                <View style={{ flex: 1 }}>
                  <Text style={[styles.label, o.checked && { color: colors.green }, o.destructive && { color: '#FF8A80' }]}>
                    {o.label}
                  </Text>
                  {o.detail ? <Text style={styles.detail}>{o.detail}</Text> : null}
                </View>
                {o.checked ? <Icon name="check" size={22} color={colors.green} /> : null}
              </Pressable>
            ))}
          </ScrollView>
        )}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: '#000000AA' },
  sheet: { backgroundColor: colors.card, borderTopLeftRadius: 24, borderTopRightRadius: 24, paddingTop: 10 },
  handle: { alignSelf: 'center', width: 40, height: 4, borderRadius: 2, backgroundColor: colors.track, marginBottom: 12 },
  title: { color: colors.text, fontSize: 18, fontWeight: '700', paddingHorizontal: 24, paddingBottom: 8 },
  option: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 24, paddingVertical: 14, minHeight: 52 },
  label: { color: colors.text, fontSize: 16, writingDirection: 'auto' },
  detail: { color: colors.faint, fontSize: 13, marginTop: 2 },
});
