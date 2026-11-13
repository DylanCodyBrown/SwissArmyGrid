/**
 * settingsWindow.ts: the Settings window (the cog at the far left of the tab bar). [WIRE + EXPORT]
 *
 * The theme and whether column headers show their profile, remembered per viewer and never sent
 * anywhere; and what the agent has done (agentAccess.ts, from the server's audit log). How a
 * table compares text is a table setting, so it lives on the ribbon's Table row (textMatching.ts).
 * Everything here applies as you change it, so the window only has Close.
 *
 * ROUTING: [SETTINGS BUTTON]      -> openSettings()
 *          [THEME CHOICE]         -> setTheme(choice)  (every open tab and window follows)
 *          [COLUMN PROFILES]      -> setHeaderProfiles(on)  (off by default)
 *          [CONNECT AN AGENT]     Copy -> copyMcpPrompt()  (a prompt that installs the MCP in any agent)
 *          [CLOSE]                -> close()
 *          [AGENT ACCESS]         -> agentAccess.agentAccessSection()  (agent activity)
 */
import { agentAccessSection } from "./agentAccess";
import { button, h, openDialog, toast } from "./dom";
import { THEME_LABELS, headerProfiles, setHeaderProfiles, setTheme, themeChoice, type ThemeChoice } from "./theme";

export function settingsButton(): HTMLButtonElement {
  // First in the Application row: always there, whichever tab is open.
  const btn = button("Settings", () => openSettings(), { icon: "cog", class: "ribbon-btn", title: "Settings: theme, agent setup and activity" });
  btn.setAttribute("aria-haspopup", "dialog");
  return btn;
}

/** What an agent needs to connect: paste it into Claude Code (or any agent that has a shell). The
 * command is the one the installers put on PATH (uv tool install of the wheel). */
export const MCP_PROMPT = [
  "Connect the grid MCP server so you can work on my local data grids:",
  "1. Run: claude mcp add --scope user swissarmygrid -- swissarmygrid-mcp",
  "   (another agent: add an MCP server over stdio whose command is swissarmygrid-mcp)",
  "2. Reconnect your MCP servers (/mcp in Claude Code).",
  "3. Call its guide tool first; it explains the grids and the tools.",
].join("\n");

async function copyMcpPrompt(): Promise<void> {
  try {
    await navigator.clipboard.writeText(MCP_PROMPT);
    toast("Copied. Paste it to your agent.");
  } catch {
    toast("The browser blocked the clipboard. Select the text and copy it.", "error");
  }
}

export function openSettings(): void {
  const { body, foot, close } = openDialog("Settings", true);
  const themes = Object.entries(THEME_LABELS).map(([value, label]) =>
    h(
      "label",
      { class: "check" },
      h("input", { type: "radio", name: "theme", value, checked: value === themeChoice(), onchange: () => setTheme(value as ThemeChoice) }),
      label,
    ),
  );
  const profiles = h("input", { type: "checkbox", checked: headerProfiles(), onchange: () => setHeaderProfiles(profiles.checked) });
  body.append(
    h(
      "fieldset",
      { class: "fieldset" },
      h("legend", {}, "Grid (this browser)"),
      h(
        "label",
        { class: "choice" },
        profiles,
        h("span", {}, h("strong", {}, "Show column profiles in the headers"), h("small", {}, "Each header adds its distinct count, its share of blanks and a small histogram. Off: plain headers, and no profiling while you work.")),
      ),
    ),
    h(
      "fieldset",
      { class: "fieldset" },
      h("legend", {}, "Theme (this browser)"),
      h("p", { class: "hint" }, "System follows Windows' light, dark and contrast settings."),
      h("div", { class: "form-grid", role: "radiogroup", "aria-label": "Theme" }, ...themes),
    ),
    h(
      "fieldset",
      { class: "fieldset" },
      h("legend", {}, "Connect an agent"),
      h("p", { class: "hint" }, "A prompt that installs the grid's MCP server in Claude Code or any agent with a shell."),
      h("pre", { class: "mono mcp-prompt" }, MCP_PROMPT),
      button("Copy the setup prompt", () => void copyMcpPrompt(), { icon: "copy" }),
    ),
    agentAccessSection(),
  );
  foot.append(button("Close", close, { class: "primary" }));
  (body.querySelector("input:checked") as HTMLInputElement | null)?.focus();
}
