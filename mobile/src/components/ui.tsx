import { MaterialCommunityIcons } from '@expo/vector-icons';
import { Image } from 'expo-image';
import type { ComponentProps, ReactNode } from 'react';
import { Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';
import type { Book } from '../lib/library';
import { colors, serif } from '../lib/theme';

export type IconName = ComponentProps<typeof MaterialCommunityIcons>['name'];

export function Icon({ name, size = 24, color = colors.text }: { name: IconName; size?: number; color?: string }) {
  return <MaterialCommunityIcons name={name} size={size} color={color} />;
}

export function IconButton({
  icon,
  label,
  onPress,
  size = 48,
  primary = false,
  disabled = false,
  style,
}: {
  icon: IconName;
  label: string;
  onPress: () => void;
  size?: number;
  primary?: boolean;
  disabled?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      android_ripple={{ color: '#78EFA333', borderless: true, radius: size / 2 }}
      style={({ pressed }) => [
        { width: size, height: size, borderRadius: size / 2, alignItems: 'center', justifyContent: 'center' },
        primary && { backgroundColor: colors.green },
        { opacity: disabled ? 0.3 : pressed ? 0.7 : 1 },
        style,
      ]}>
      <Icon name={icon} size={Math.round(size * (primary ? 0.48 : 0.52))} color={primary ? colors.bg : colors.text} />
    </Pressable>
  );
}

export function Button({ label, onPress, primary = true }: { label: string; onPress: () => void; primary?: boolean }) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.button, !primary && { backgroundColor: colors.raised }, pressed && { opacity: 0.8 }]}>
      <Text style={[styles.buttonText, !primary && { color: colors.text }]}>{label}</Text>
    </Pressable>
  );
}

/** A square-ish tool button: icon above a short label. */
export function Tool({ icon, label, onPress, active = false }: { icon: IconName; label: string; onPress: () => void; active?: boolean }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      style={({ pressed }) => [styles.tool, pressed && { opacity: 0.75 }]}>
      <Icon name={icon} size={20} color={active ? colors.green : colors.muted} />
      <Text numberOfLines={1} style={[styles.toolText, active && { color: colors.green }]}>
        {label}
      </Text>
    </Pressable>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <Text style={styles.eyebrow}>{children}</Text>;
}

export function ProgressLine({ value, height = 4, style }: { value: number; height?: number; style?: StyleProp<ViewStyle> }) {
  return (
    <View style={[{ height, borderRadius: height, backgroundColor: colors.track, overflow: 'hidden' }, style]}>
      <View style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, height, backgroundColor: colors.green }} />
    </View>
  );
}

export function Cover({ book, width, height, radius = 8 }: { book: Book; width: number; height: number; radius?: number }) {
  const box = { width, height, borderRadius: radius, overflow: 'hidden' as const };
  if (book.cover) return <Image source={{ uri: book.cover }} style={box} contentFit="cover" />;
  const small = width < 48;
  return (
    <View style={[box, styles.placeholder]}>
      <Text numberOfLines={small ? 1 : 5} style={[styles.placeholderText, { fontSize: width >= 120 ? 20 : width >= 70 ? 12 : small ? 16 : 10 }]}>
        {small ? book.title.slice(0, 1) : book.title}
      </Text>
    </View>
  );
}

export function EmptyState({
  icon,
  title,
  message,
  action,
  onAction,
}: {
  icon: IconName;
  title: string;
  message: string;
  action: string;
  onAction: () => void;
}) {
  return (
    <View style={styles.empty}>
      <View style={styles.emptyArt}>
        <Icon name={icon} size={52} color={colors.green} />
      </View>
      <Text style={styles.emptyTitle}>{title}</Text>
      <Text style={styles.emptyText}>{message}</Text>
      <View style={{ marginTop: 24 }}>
        <Button label={action} onPress={onAction} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  button: { minHeight: 52, paddingHorizontal: 26, borderRadius: 26, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center' },
  buttonText: { color: colors.bg, fontSize: 15, fontWeight: '700' },
  tool: { flex: 1, height: 64, marginHorizontal: 3, borderRadius: 18, backgroundColor: colors.raised, alignItems: 'center', justifyContent: 'center', gap: 4, paddingHorizontal: 4 },
  toolText: { color: colors.text, fontSize: 12 },
  eyebrow: { color: colors.muted, fontSize: 12, fontWeight: '700', letterSpacing: 1.4, marginTop: 24, marginBottom: 10 },
  placeholder: { backgroundColor: '#2A4A38', alignItems: 'center', justifyContent: 'center', padding: 4 },
  placeholderText: { color: '#E9EFDF', fontFamily: serif, fontWeight: '700', textAlign: 'center' },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 32, paddingVertical: 48 },
  emptyArt: { width: 104, height: 104, borderRadius: 52, backgroundColor: colors.greenDim, alignItems: 'center', justifyContent: 'center' },
  emptyTitle: { color: colors.text, fontFamily: serif, fontWeight: '700', fontSize: 22, marginTop: 20, marginBottom: 8, textAlign: 'center' },
  emptyText: { color: colors.muted, fontSize: 15, lineHeight: 22, textAlign: 'center' },
});
