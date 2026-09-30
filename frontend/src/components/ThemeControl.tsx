/**
 * App-wide theme preference (System / Dark / Light). It lives on both venues' Settings
 * pages because it is not a venue setting: it changes the whole app, both venues.
 */
import { useTheme, type ThemePref } from "../lib/theme";
import { Segmented } from "./ui";

export function ThemeControl() {
  const { pref, setPref } = useTheme();
  return (
    <>
      <Segmented<ThemePref>
        label="Theme (applies to both venues)"
        value={pref}
        onChange={setPref}
        options={[
          { value: "system", label: "System" },
          { value: "dark", label: "Dark" },
          { value: "light", label: "Light" },
        ]}
      />
      <div className="muted">Applies to the whole app — Kalshi and Coinbase pages alike.</div>
    </>
  );
}
