import { DarkTheme, ThemeProvider } from '@react-navigation/native';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import HomeScreen from './screens/HomeScreen';
import MatchDetailScreen from './screens/MatchDetailScreen';
import SettingsScreen from './screens/SettingsScreen';
import { Colors } from './theme/colors';

export type RootStackParamList = {
  Home: undefined;
  MatchDetail: { matchId: string };
  Settings: undefined;
};

const Stack = createNativeStackNavigator<RootStackParamList>();

const stratumDarkTheme = {
  ...DarkTheme,
  colors: {
    ...DarkTheme.colors,
    primary: Colors.primary,
    background: Colors.background,
    card: Colors.surface,
    text: Colors.textPrimary,
    border: Colors.border,
    notification: Colors.accentPink,
  },
};

export default function App() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <ThemeProvider value={stratumDarkTheme}>
        <StatusBar style="light" />
        <NavigationContainer theme={stratumDarkTheme}>
          <Stack.Navigator
            screenOptions={{
              headerStyle: { backgroundColor: Colors.surface },
              headerTintColor: Colors.textPrimary,
              headerTitleStyle: { fontWeight: '700', letterSpacing: 1 },
              contentStyle: { backgroundColor: Colors.background },
            }}
          >
            <Stack.Screen
              name="Home"
              component={HomeScreen}
              options={{ title: 'STRATUM', headerRight: () => null }}
            />
            <Stack.Screen
              name="MatchDetail"
              component={MatchDetailScreen}
              options={({ route }) => ({ title: 'MARKETS' })}
            />
            <Stack.Screen
              name="Settings"
              component={SettingsScreen}
              options={{ title: 'SETTINGS' }}
            />
          </Stack.Navigator>
        </NavigationContainer>
      </ThemeProvider>
    </GestureHandlerRootView>
  );
}
