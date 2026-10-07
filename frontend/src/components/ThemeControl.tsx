/** App-wide theme preference (System / Dark / Light). */
import { useTheme, type ThemePref } from "../lib/theme";
import { Segmented } from "./ui";

export function ThemeControl() {
  const { pref, setPref } = useTheme();
  return (
    <>
      <Segmented<ThemePref>
        label="Theme"
        value={pref}
        onChange={setPref}
        options={[
          { value: "system", label: "System" },
          { value: "dark", label: "Dark" },
          { value: "light", label: "Light" },
        ]}
      />
      <div className="muted">Applies to the whole app.</div>
    </>
  );
}
