// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace TheWarriorsMapTriggers;

internal sealed class RuntimeSession : IDisposable
{
    private readonly SessionLogger logger;
    private readonly AppSettings settings;
    private readonly string root;
    private readonly string python;
    private bool stopRequested;
    private bool cleanupFailed;
    private Process? backend;
    public string DirectoryPath { get; }
    public bool BlocksRetained { get; private set; }
    public event Action<string>? Output;

    public RuntimeSession(SessionLogger logger, AppSettings settings, string? distributionRoot = null)
    {
        this.logger = logger;
        this.settings = settings;
        root = distributionRoot ?? AppContext.BaseDirectory;
        python = Path.Combine(root, "runtime", "python.exe");
        DirectoryPath = Path.Combine(AppSettings.DataDirectory, "sessions", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(DirectoryPath);
    }

    private ProcessStartInfo PythonStart(string script, params string[] arguments)
    {
        var info = StartInfo(python);
        foreach (string option in new[] { "-E", "-s", "-B", script }) info.ArgumentList.Add(option);
        foreach (string argument in arguments) info.ArgumentList.Add(argument);
        return info;
    }

    private ProcessStartInfo StartInfo(string executable)
    {
        var info = new ProcessStartInfo(executable)
        {
            WorkingDirectory = Path.Combine(root, "backend"), UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
        };
        info.Environment.Remove("PYTHONHOME"); info.Environment.Remove("PYTHONPATH");
        info.Environment["PYTHONNOUSERSITE"] = "1";
        info.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        info.Environment["TW_MAPTRIGGERS_PARENT_PID"] = Environment.ProcessId.ToString();
        return info;
    }

    public void ValidateRuntime()
    {
        foreach (string relative in new[] { "runtime/python.exe", "runtime/pythonw.exe", "backend/prepare.py", "backend/research/map_triggers_collision_freecam.ps1" })
            if (!File.Exists(Path.Combine(root, relative)))
                throw new FileNotFoundException($"The distribution is incomplete: {relative}. Extract the complete ZIP.");
    }

    public async Task RunAsync()
    {
        ValidateRuntime(); settings.Validate();
        Send("Reading the selected image and preparing its private map cache…");
        using var preparation = new Process { StartInfo = PythonStart(Path.Combine(root, "backend", "prepare.py"),
            "--iso", settings.IsoPath, "--cache", Path.Combine(AppSettings.DataDirectory, "cache")) };
        preparation.Start();
        Task<string> resultTask = preparation.StandardOutput.ReadToEndAsync();
        Task<string> errorTask = preparation.StandardError.ReadToEndAsync();
        await preparation.WaitForExitAsync();
        string result = await resultTask; string error = await errorTask;
        if (preparation.ExitCode != 0) throw new InvalidOperationException("Image preparation failed. " + error);
        if (stopRequested) return;
        using JsonDocument prepared = JsonDocument.Parse(result);
        string catalog = prepared.RootElement.GetProperty("catalog").GetString()!;
        string iso = prepared.RootElement.GetProperty("iso").GetString()!;
        await Task.Run(() => PreflightResult.Run(settings.PinePort));
        if (stopRequested) return;
        using var udpReservation = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0));
        int cameraPort = ((IPEndPoint)udpReservation.Client.LocalEndPoint!).Port;
        udpReservation.Close();
        var info = StartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"));
        foreach (string argument in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            Path.Combine(root, "backend", "research", "map_triggers_collision_freecam.ps1"),
            "-CollisionToggle", "-Controller", "-NonInteractive", "-IncludeAuxiliary", "-CatalogFile", catalog, "-Iso", iso,
            "-SessionDirectory", DirectoryPath, "-PythonExecutable", python,
            "-PythonwExecutable", Path.Combine(root, "runtime", "pythonw.exe"),
            "-Port", settings.PinePort.ToString(), "-CameraStatePort", cameraPort.ToString(), "-Profile", settings.Profile }) info.ArgumentList.Add(argument);
        if (settings.CaptureKeyboardPad) info.ArgumentList.Add("-CaptureKeyboardPad");
        if (settings.ReturnToFollowCamera) info.ArgumentList.Add("-ReturnToFollowCamera");
        if (settings.StartWorldPaused) info.ArgumentList.Add("-StartWorldPaused");
        if (settings.ExtendedCollision) info.ArgumentList.Add("-ExtendedCollisionView");
        using var process = new Process { StartInfo = info };
        backend = process;
        process.OutputDataReceived += (_, args) => { if (args.Data is not null) Send(args.Data); };
        process.ErrorDataReceived += (_, args) => { if (args.Data is not null) Send(args.Data); };
        process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
        await process.WaitForExitAsync();
        process.WaitForExit();
        backend = null;
        logger.Info("backend_exit", new { process.ExitCode, cleanupFailed });
        BlocksRetained = process.ExitCode == 79;
        if (cleanupFailed || (process.ExitCode != 0 && process.ExitCode != 79 && !(stopRequested && process.ExitCode == 76)))
            throw new InvalidOperationException("MapTriggers could not finish cleanly. Review the log; restart the game before retrying.");
    }

    private void Send(string message)
    {
        if (message.Contains("CRITICAL", StringComparison.OrdinalIgnoreCase) || message.Contains("could not restore", StringComparison.OrdinalIgnoreCase)) cleanupFailed = true;
        logger.Info("backend", new { message }); Output?.Invoke(message);
    }

    public void RequestStop()
    {
        SessionStopSignal.Request(DirectoryPath);
        stopRequested = true;
    }

    public void Dispose() { if (backend is not null) RequestStop(); }
}
