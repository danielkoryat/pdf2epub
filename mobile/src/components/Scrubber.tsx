import { type ComponentRef, useRef, useState } from 'react';
import { type GestureResponderEvent, View } from 'react-native';
import { colors } from '../lib/theme';

/**
 * A thick progress bar that is easy to grab: touch anywhere on it and drag. Reports the value while
 * dragging (to show where the book will continue) and once more when released.
 */
export function Scrubber({
  value,
  max,
  onDrag,
  onDone,
}: {
  value: number;
  max: number;
  onDrag: (value: number | null) => void;
  onDone: (value: number) => void;
}) {
  const [width, setWidth] = useState(1);
  const [dragged, setDragged] = useState<number | null>(null);
  const left = useRef(0);
  const view = useRef<ComponentRef<typeof View>>(null);

  const measure = () =>
    view.current?.measureInWindow((x: number) => {
      left.current = x;
    });
  const at = (e: GestureResponderEvent) => Math.max(0, Math.min(1, (e.nativeEvent.pageX - left.current) / width)) * max;
  const move = (e: GestureResponderEvent) => {
    const v = at(e);
    setDragged(v);
    onDrag(v);
  };
  const end = () => {
    setDragged(null);
    onDrag(null);
  };

  const fraction = max > 0 ? Math.max(0, Math.min(1, (dragged ?? value) / max)) : 0;
  return (
    <View
      ref={view}
      onLayout={(e) => {
        setWidth(Math.max(1, e.nativeEvent.layout.width));
        measure();
      }}
      onStartShouldSetResponder={() => true}
      onMoveShouldSetResponder={() => true}
      onResponderTerminationRequest={() => false}
      onResponderGrant={(e) => {
        measure();
        move(e);
      }}
      onResponderMove={move}
      onResponderRelease={(e) => {
        const v = at(e);
        end();
        onDone(v);
      }}
      onResponderTerminate={end}
      accessibilityRole="adjustable"
      accessibilityLabel="Playback position"
      style={{ height: 48, justifyContent: 'center' }}>
      <View style={{ height: 6, borderRadius: 3, backgroundColor: colors.track }}>
        <View style={{ width: `${fraction * 100}%`, height: 6, borderRadius: 3, backgroundColor: colors.green }} />
      </View>
      <View
        pointerEvents="none"
        style={{
          position: 'absolute',
          left: fraction * width - (dragged === null ? 11 : 14),
          width: dragged === null ? 22 : 28,
          height: dragged === null ? 22 : 28,
          borderRadius: 14,
          backgroundColor: colors.green,
        }}
      />
    </View>
  );
}
