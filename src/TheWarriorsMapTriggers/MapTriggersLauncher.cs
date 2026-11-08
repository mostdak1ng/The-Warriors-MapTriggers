// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng
using System.Diagnostics;
using System.Text.Json;

namespace TheWarriorsMapTriggers;

internal sealed class MapTriggersLauncher : Form
{
    private readonly SessionLogger logger;
    private readonly TextBox iso = new() { Dock = DockStyle.Fill };
    private readonly NumericUpDown port = new() { Minimum = 1, Maximum = 65535, Value = 28011, Width = 90 };
    private readonly ComboBox profile = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 180 };
    private readonly CheckBox capture = new() { Text = "Capture Pad 1 in keyboard/mouse mode", AutoSize = true };
    private readonly CheckBox follow = new() { Text = "Return to player FollowCamera when leaving Freecam", AutoSize = true };
    private readonly CheckBox paused = new() { Text = "Start with the world practically paused", AutoSize = true };
    private readonly CheckBox extended = new() { Text = "Experimental collision: X-ray and nearby object bounds", AutoSize = true };
    private readonly CheckBox warning = new() { Text = "I have a backup. I will not create or load savestates while MapTriggers is running.", AutoSize = true };
    private readonly Button start = new() { Text = "Start MapTriggers", AutoSize = true };
    private readonly Button stop = new() { Text = "Stop safely", AutoSize = true, Enabled = false };
    private readonly Button recheck = new() { Text = "Check PCSX2", AutoSize = true };
    private readonly Label status = new() { AutoSize = true, Text = "Ready to connect to PCSX2." };
    private readonly TextBox output = new() { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
    private readonly System.Windows.Forms.Timer mapTimer = new() { Interval = 250 };
    private RuntimeSession? session;
    private bool closing;
    private bool selectingMap;
    private readonly MapSelectionInbox mapInbox = new();

    public MapTriggersLauncher(SessionLogger logger)
    {
        this.logger = logger;
        Text = $"{BuildInfo.ProductName} v{BuildInfo.Version}";
        ClientSize = new Size(850, 690); MinimumSize = new Size(780, 640);
        StartPosition = FormStartPosition.CenterScreen; Font = new Font("Segoe UI", 10f);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), ColumnCount = 1, RowCount = 10 };
        layout.Controls.Add(new Label { Text = "THE WARRIORS MAPTRIGGERS", AutoSize = true, Font = new Font("Segoe UI Semibold", 21) });
        layout.Controls.Add(new Label { Text = "USA SLUS-21215 v1.03 · CRC B99A75DE · Borderless fullscreen or windowed", AutoSize = true });
        var imageRow = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 3, AutoSize = true };
        imageRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        imageRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        imageRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        imageRow.Controls.Add(new Label { Text = "Your game ISO", AutoSize = true }); imageRow.Controls.Add(iso);
        var browse = new Button { Text = "Browse…", AutoSize = true };
        browse.Click += (_, _) => { using var dialog = new OpenFileDialog { Filter = "ISO9660 image (*.iso)|*.iso", CheckFileExists = true }; if (dialog.ShowDialog(this) == DialogResult.OK) iso.Text = dialog.FileName; };
        imageRow.Controls.Add(browse); layout.Controls.Add(imageRow);
        var options = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top };
        profile.Items.AddRange(["MapTriggers", "Freecam"]); profile.SelectedIndex = 0;
        options.Controls.AddRange([new Label { Text = "Start profile", AutoSize = true }, profile, new Label { Text = "PINE port", AutoSize = true }, port]);
        layout.Controls.Add(options);
        var checks = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        checks.Controls.AddRange([capture, follow, paused, extended]); layout.Controls.Add(checks);
        layout.Controls.Add(new Label { Text = "Map data is generated locally from your ISO. It is never uploaded.\nController: Select+L3 · Pause: P / Select+Square · Carry: F8 / Select+R3 · Exit: F10", AutoSize = true });
        layout.Controls.Add(warning);
        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top };
        var logs = new Button { Text = "Open logs", AutoSize = true };
        logs.Click += (_, _) => Process.Start(new ProcessStartInfo("explorer.exe", Path.GetDirectoryName(logger.Path)!) { UseShellExecute = true });
        actions.Controls.AddRange([start, stop, recheck, logs]); layout.Controls.Add(actions); layout.Controls.Add(status);
        for (int i = 0; i < 9; i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.Controls.Add(output); Controls.Add(layout);
        try { ApplySettings(AppSettings.Load()); } catch (Exception error) { Append("Settings were not loaded: " + error.Message); }
        start.Click += async (_, _) => await StartAsync();
        stop.Click += (_, _) => { session?.RequestStop(); status.Text = "Stopping safely; waiting for native restoration…"; };
        recheck.Click += async (_, _) => await CheckAsync();
        mapTimer.Tick += (_, _) => CheckMapRequest(); mapTimer.Start();
        FormClosing += (_, args) => { if (session is not null) { args.Cancel = true; closing = true; session.RequestStop(); status.Text = "Restoring native state before closing…"; } };
    }

    private AppSettings ReadSettings() => new()
    {
        IsoPath = iso.Text.Trim(), PinePort = (int)port.Value, Profile = profile.SelectedIndex == 1 ? "freecam" : "maptriggers",
        CaptureKeyboardPad = capture.Checked, ReturnToFollowCamera = follow.Checked,
        StartWorldPaused = paused.Checked, ExtendedCollision = extended.Checked,
    };

    private void ApplySettings(AppSettings settings)
    {
        iso.Text = settings.IsoPath; port.Value = settings.PinePort; profile.SelectedIndex = settings.Profile == "freecam" ? 1 : 0;
        capture.Checked = settings.CaptureKeyboardPad; follow.Checked = settings.ReturnToFollowCamera;
        paused.Checked = settings.StartWorldPaused; extended.Checked = settings.ExtendedCollision;
    }

    private async Task CheckAsync()
    {
        recheck.Enabled = start.Enabled = false;
        try
        {
            var result = await Task.Run(() => PreflightResult.Run((int)port.Value));
            status.Text = result.WorldReady ? "Ready: supported game, camera, player and hooks verified."
                : "Supported game and hooks verified. Waiting for a playable world; O and F10 remain available.";
            logger.Info("preflight_passed", result);
        }
        catch (Exception error) { status.Text = "Not ready: " + error.Message; logger.Error("preflight_failed", error); }
        finally { recheck.Enabled = start.Enabled = session is null; }
    }

    private async Task StartAsync()
    {
        if (session is not null) return;
        if (!warning.Checked) { status.Text = "Confirm the savestate backup before starting."; return; }
        if (!File.Exists(iso.Text.Trim())) { status.Text = "Choose your supported game ISO first."; return; }
        AppSettings settings = ReadSettings();
        try { settings.Save(); } catch (Exception error) { status.Text = "Could not save settings: " + error.Message; return; }
        using var running = new RuntimeSession(logger, settings); session = running;
        running.Output += message => { if (!IsDisposed) BeginInvoke(() => Append(message)); };
        SetRunning(true);
        try { await running.RunAsync(); status.Text = running.BlocksRetained ? "Stopped with blocks retained. Restart the game before another session." : "MapTriggers stopped. Native hooks restored."; }
        catch (Exception error) { logger.Error("session_failed", error); Append(error.Message); status.Text = "Session failed. Review the log before retrying."; closing = false; }
        finally { session = null; SetRunning(false); }
        if (closing) Close();
    }

    private void SetRunning(bool running)
    {
        start.Enabled = recheck.Enabled = !running; stop.Enabled = running;
        iso.Enabled = port.Enabled = profile.Enabled = capture.Enabled = follow.Enabled = paused.Enabled = extended.Enabled = !running;
    }

    private void Append(string message)
    {
        if (output.TextLength > 100_000) output.Text = output.Text[^50_000..];
        output.AppendText(message + Environment.NewLine);
    }

    private void CheckMapRequest()
    {
        if (session is null || selectingMap) return;
        string request = Path.Combine(session.DirectoryPath, "map_request.json");
        if (!File.Exists(request)) return;
        selectingMap = true;
        try
        {
            MapSelectionRequest? selection = mapInbox.Claim(File.ReadAllText(request));
            if (selection is null) return;
            MapChoice[] choices = selection.Choices;
            using var dialog = new Form { Text = "Select the loaded map", Width = 560, Height = 160, StartPosition = FormStartPosition.CenterParent };
            var maps = new ComboBox { Dock = DockStyle.Top, DropDownStyle = ComboBoxStyle.DropDownList, DataSource = choices, DisplayMember = "Name" };
            var choose = new Button { Text = "Use this map", Dock = DockStyle.Bottom, DialogResult = DialogResult.OK };
            dialog.Controls.AddRange([maps, choose]); dialog.AcceptButton = choose;
            if (dialog.ShowDialog(this) == DialogResult.OK && maps.SelectedItem is MapChoice choice)
            {
                string temporary = Path.Combine(session.DirectoryPath, "map_response.tmp");
                File.WriteAllText(temporary, MapSelectionInbox.Response(selection, choice)); File.Move(temporary, Path.Combine(session.DirectoryPath, "map_response.txt"), true);
            }
            else session.RequestStop();
        }
        catch (IOException) { }
        catch (JsonException) { }
        finally { selectingMap = false; }
    }

    protected override void Dispose(bool disposing) { if (disposing) mapTimer.Dispose(); base.Dispose(disposing); }
}
