import { StatusBar } from 'expo-status-bar';
import { Tabs } from 'expo-router/tabs';
import { useEffect } from 'react';
import { View } from 'react-native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';
import { BottomBar } from '../components/BottomBar';
import { ImportBanner } from '../components/ImportBanner';
import { load } from '../lib/library';
import { startPlayer } from '../lib/player';
import { colors } from '../lib/theme';

export default function Layout() {
  useEffect(() => {
    load().then(startPlayer);
  }, []);
  return (
    <SafeAreaProvider>
      <StatusBar style="light" />
      <Shell />
    </SafeAreaProvider>
  );
}

/** Below the status bar: the import panel when there is one, the open tab, then the bottom bar. */
function Shell() {
  const insets = useSafeAreaInsets();
  return (
    <View style={{ flex: 1, backgroundColor: colors.bg, paddingTop: insets.top }}>
      <ImportBanner />
      <Tabs
        tabBar={(props) => <BottomBar {...props} />}
        screenOptions={{ headerShown: false, sceneStyle: { backgroundColor: colors.bg } }}>
        <Tabs.Screen name="index" options={{ title: 'Library' }} />
        <Tabs.Screen name="listen" options={{ title: 'Listen' }} />
        <Tabs.Screen name="read" options={{ title: 'Read' }} />
      </Tabs>
    </View>
  );
}
