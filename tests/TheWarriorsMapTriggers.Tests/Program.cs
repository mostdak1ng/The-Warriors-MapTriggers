// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Buffers.Binary;
using System.Numerics;
using TheWarriorsMapTriggers;

var tests = new List<(string Name, Action Run)>
{
    ("Build metadata", TestBuildMetadata),
    ("Repeated stop and cleanup stop", TestStopSignal),
    ("Preflight conflicts", TestPreflightConflicts),
    ("Map selection handshake", TestMapSelection),
    ("Supported identity", TestSupportedIdentity),
    ("Radial deadzone", TestRadialDeadzone),
    ("Quaternion rotation", TestQuaternionRotation),
    ("MIPS helpers", TestMipsHelpers),
    ("Native handle logging", TestNativeHandleLogging),
    ("Persistent distribution settings", TestSettings),
};
if (args.Contains("--live-preflight", StringComparer.OrdinalIgnoreCase))
{
    tests.Add(("Live read-only preflight", TestLivePreflight));
}
int failures = 0;
foreach ((string name, Action run) in tests)
{
    try
    {
        run();
        Console.WriteLine($"PASS  {name}");
    }
    catch (Exception error)
    {
        failures++;
        Console.Error.WriteLine($"FAIL  {name}: {error.Message}");
    }
}

Console.WriteLine($"{tests.Count - failures}/{tests.Count} tests passed.");
return failures == 0 ? 0 : 1;

static void TestBuildMetadata()
{
    Equal("0.3.2", BuildInfo.Version);
    Equal("The Warriors MapTriggers", BuildInfo.ProductName);
    Equal("GPL-3.0-only", BuildInfo.License);
    Equal(0x005D9150u, GameAddresses.CameraPriorityPointer);
    Equal(0x005D9158u, GameAddresses.FollowCameraPointer);
    Equal(0x005102CCu, GameAddresses.WorldTimestep);
}

static void TestSettings()
{
    string directory = Path.Combine(AppContext.BaseDirectory, "settings-ñ-" + Guid.NewGuid().ToString("N"));
    AppSettings settings = new() { IsoPath = @"C:\Games with spaces\The Warriors ñ.iso", PinePort = 31001,
        Profile = "freecam", CaptureKeyboardPad = true, ReturnToFollowCamera = true, StartWorldPaused = true };
    Throws<InvalidDataException>(() => (settings with { PinePort = 0 }).Save(directory));
    True(!Directory.Exists(directory));
    settings.Save(directory);
    Equal(settings, AppSettings.Load(directory));
    Throws<InvalidDataException>(() => (settings with { Profile = "unknown" }).Save(directory));
    Equal(settings, AppSettings.Load(directory));
    True(!File.Exists(Path.Combine(directory, "settings.json.tmp")));
}

static void TestSupportedIdentity()
{
    var supported = new GameIdentity(
        "2.7.522", "The Warriors", "slus-21215", "1.03", "B99A75DE", PineStatus.Running);
    True(supported.IsSupported);
    supported.EnsureSupported();

    var wrong = supported with { Crc = "00000000" };
    True(!wrong.IsSupported);
    Throws<NotSupportedException>(wrong.EnsureSupported);
}


static void TestRadialDeadzone()
{
    Equal(Vector2.Zero, CameraMath.ApplyRadialDeadzone(Vector2.Zero, 0.10f));
    Equal(
        Vector2.Zero,
        CameraMath.ApplyRadialDeadzone(new Vector2(0.06f, 0.07f), 0.10f));
    Vector2 half = CameraMath.ApplyRadialDeadzone(new Vector2(0.55f, 0f), 0.10f);
    Near(0.5f, half.X, 0.0001f);
    Near(0f, half.Y, 0.0001f);
    Vector2 clamped = CameraMath.ApplyRadialDeadzone(new Vector2(2f, 0f), 0.10f);
    Near(1f, clamped.X, 0.0001f);
    Throws<ArgumentOutOfRangeException>(() =>
        CameraMath.ApplyRadialDeadzone(Vector2.One, 1f));
}

static void TestQuaternionRotation()
{
    Quaternion yaw = CameraMath.AxisAngle(Vector3.UnitZ, MathF.PI / 2f);
    Vector3 right = CameraMath.Rotate(yaw, Vector3.UnitX);
    Near(0f, right.X, 0.0001f);
    Near(1f, right.Y, 0.0001f);
    Near(0f, right.Z, 0.0001f);
    Quaternion normalized = CameraMath.Multiply(yaw, Quaternion.Identity);
    Near(1f, normalized.Length(), 0.0001f);
    Throws<InvalidDataException>(() =>
        CameraMath.NormalizeQuaternion(Quaternion.Zero));
}

static void TestMipsHelpers()
{
    Equal(0x0C142A04u, Mips.JumpAndLink(GameAddresses.AllocatorBootstrapScratch));
    Equal(0x27BDFFE0u, Mips.AddImmediateUnsigned(29, 29, -0x20));
    Equal(0x03E00008u, Mips.JumpRegister(31));
}


static void TestLivePreflight()
{
    PreflightResult result = PreflightResult.Run();
    True(result.Identity.IsSupported);
    True(CameraMath.IsFinite(result.CameraPosition));
    True(CameraMath.IsFinite(result.PlayerPosition));
    Console.WriteLine(
        $"      PCSX2 {result.Identity.EmulatorVersion}; " +
        $"{result.Identity.Serial} v{result.Identity.GameVersion}; " +
        $"CRC {result.Identity.Crc.ToUpperInvariant()}; " +
        $"status {result.Identity.Status}");
}


static void TestNativeHandleLogging()
{
    string directory = Path.Combine(
        AppContext.BaseDirectory, "test-logs", Guid.NewGuid().ToString("N"));
    try
    {
        string path;
        using (SessionLogger logger = SessionLogger.Create(directory))
        {
            path = logger.Path;
            logger.Info("native_handle", new { handle = (nint)0x1234 });
        }

        string line = File.ReadAllText(path);
        True(line.Contains("\"handle\":\"0x1234\"", StringComparison.Ordinal));
        True(!line.Contains("serialization_failed", StringComparison.Ordinal));
    }
    finally
    {
        if (Directory.Exists(directory))
        {
            Directory.Delete(directory, recursive: true);
        }
    }
}





static void True(bool condition)
{
    if (!condition)
    {
        throw new InvalidOperationException("Expected condition to be true.");
    }
}

static void Equal<T>(T expected, T actual) where T : notnull
{
    if (!EqualityComparer<T>.Default.Equals(expected, actual))
    {
        throw new InvalidOperationException(
            $"Expected {expected}, received {actual}.");
    }
}

static void Near(float expected, float actual, float tolerance)
{
    if (MathF.Abs(expected - actual) > tolerance)
    {
        throw new InvalidOperationException(
            $"Expected {expected} ± {tolerance}, received {actual}.");
    }
}

static void Throws<TException>(Action action) where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException(
        $"Expected exception {typeof(TException).Name}.");
}

static void TestPreflightConflicts()
{
    PreflightResult.ValidateInstructionValue(GameAddresses.BootstrapHookOriginal,
        GameAddresses.BootstrapHookOriginal, GameAddresses.BootstrapHook, "allocator");
    Throws<InvalidOperationException>(() => PreflightResult.ValidateInstructionValue(0,
        GameAddresses.BootstrapHookOriginal, GameAddresses.BootstrapHook, "allocator"));
    True(!GameAddresses.IsPlausibleEePointer(0));
    True(!GameAddresses.IsPlausibleHeapPointer(0x02000000));
}

static void TestMapSelection()
{
    var inbox = new MapSelectionInbox();
    string id = Guid.NewGuid().ToString("N");
    string json = System.Text.Json.JsonSerializer.Serialize(new { request_id = id,
        choices = new[] { new { id = "map-a", display_name = "Map A" } } });
    MapSelectionRequest request = inbox.Claim(json)!;
    Equal("Map A", request.Choices[0].Name);
    True(inbox.Claim(json) is null);
    using var response = System.Text.Json.JsonDocument.Parse(MapSelectionInbox.Response(request, request.Choices[0]));
    Equal(id, response.RootElement.GetProperty("request_id").GetString()!);
    Equal("map-a", response.RootElement.GetProperty("map_id").GetString()!);
    True(inbox.Claim(json.Replace(id, Guid.NewGuid().ToString("N"))) is not null);
    True(inbox.Claim(json) is null);
}

static void TestStopSignal()
{
    string directory = Path.Combine(AppContext.BaseDirectory, "stop-test-"+Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(directory);
    try
    {
        SessionStopSignal.Request(directory);
        True(File.Exists(Path.Combine(directory, "stop.flag")));
        True(!File.Exists(Path.Combine(directory, "exit_now.flag")));
        SessionStopSignal.Request(directory);
        True(File.Exists(Path.Combine(directory, "exit_now.flag")));
        File.Delete(Path.Combine(directory, "stop.flag"));
        File.Delete(Path.Combine(directory, "exit_now.flag"));
        File.WriteAllText(Path.Combine(directory, "cleanup_wait.flag"), "retiring");
        SessionStopSignal.Request(directory);
        True(File.Exists(Path.Combine(directory, "exit_now.flag")));
    }
    finally { Directory.Delete(directory, recursive: true); }
}
