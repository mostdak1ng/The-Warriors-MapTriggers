// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Text.Json;
using System.Text.Json.Serialization;

namespace TheWarriorsMapTriggers;

public sealed class SessionLogger : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        IncludeFields = true,
        WriteIndented = false,
        Converters = { new IntPtrJsonConverter() },
    };

    private readonly StreamWriter writer;
    private readonly object writeLock = new();
    private bool disposed;

    private SessionLogger(string path, StreamWriter writer)
    {
        Path = path;
        this.writer = writer;
    }

    public string Path { get; }

    public static SessionLogger Create(string? preferredDirectory = null)
    {
        string preferred = preferredDirectory ?? System.IO.Path.Combine(
            AppSettings.DataDirectory, "logs");
        string fallback = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TheWarriorsMapTriggers",
            "logs");
        try
        {
            return CreateInDirectory(preferred);
        }
        catch (Exception error) when (
            error is UnauthorizedAccessException or IOException or System.Security.SecurityException)
        {
            if (System.IO.Path.GetFullPath(preferred).Equals(
                System.IO.Path.GetFullPath(fallback), StringComparison.OrdinalIgnoreCase))
            {
                throw;
            }

            return CreateInDirectory(fallback);
        }
    }

    private static SessionLogger CreateInDirectory(string directory)
    {
        Directory.CreateDirectory(directory);
        string timestamp = DateTimeOffset.Now.ToString("yyyyMMdd-HHmmss-fff");
        string path = System.IO.Path.Combine(
            directory,
            $"TheWarriorsMapTriggers-v{BuildInfo.Version}-{timestamp}-{Environment.ProcessId}.log");
        var stream = new FileStream(
            path,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.SequentialScan);
        var writer = new StreamWriter(stream)
        {
            AutoFlush = true,
        };
        return new SessionLogger(path, writer);
    }

    public void Info(string eventName, object? data = null) =>
        Write("INFO", eventName, data, null);

    public void Warning(string eventName, object? data = null) =>
        Write("WARNING", eventName, data, null);

    public void Error(string eventName, Exception error, object? data = null) =>
        Write("ERROR", eventName, data, error);

    public void Trace(string eventName, object? data = null) =>
        Write("TRACE", eventName, data, null);

    public void Dispose()
    {
        lock (writeLock)
        {
            if (disposed)
            {
                return;
            }

            disposed = true;
            writer.Dispose();
        }
    }

    private void Write(
        string level,
        string eventName,
        object? data,
        Exception? error)
    {
        lock (writeLock)
        {
            if (disposed)
            {
                return;
            }

            var entry = new
            {
                timestamp = DateTimeOffset.Now.ToString("O"),
                utc = DateTimeOffset.UtcNow.ToString("O"),
                level,
                @event = eventName,
                data,
                exception = error?.ToString(),
            };
            string line;
            try
            {
                line = JsonSerializer.Serialize(entry, JsonOptions);
            }
            catch (Exception serializationError) when (
                serializationError is JsonException or NotSupportedException)
            {
                line = JsonSerializer.Serialize(new
                {
                    timestamp = DateTimeOffset.Now.ToString("O"),
                    utc = DateTimeOffset.UtcNow.ToString("O"),
                    level = "ERROR",
                    @event = "log_payload_serialization_failed",
                    originalLevel = level,
                    originalEvent = eventName,
                    dataType = data?.GetType().FullName,
                    dataText = data?.ToString(),
                    exception = serializationError.ToString(),
                    originalException = error?.ToString(),
                }, JsonOptions);
            }

            try
            {
                writer.WriteLine(line);
            }
            catch (Exception writeError) when (
                writeError is IOException or UnauthorizedAccessException or ObjectDisposedException)
            {
                // Diagnostics must never prevent native state cleanup.
            }
        }
    }

    private sealed class IntPtrJsonConverter : JsonConverter<nint>
    {
        public override nint Read(
            ref Utf8JsonReader reader,
            Type typeToConvert,
            JsonSerializerOptions options) =>
            throw new NotSupportedException("Log handle deserialization is not used.");

        public override void Write(
            Utf8JsonWriter writer,
            nint value,
            JsonSerializerOptions options) =>
            writer.WriteStringValue($"0x{value.ToInt64():X}");
    }
}
