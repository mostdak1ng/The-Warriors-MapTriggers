// SPDX-License-Identifier: GPL-3.0-only
using System.Text.Json;

namespace TheWarriorsMapTriggers;

public sealed record MapChoice(string Id, string Name);
public sealed record MapSelectionRequest(string RequestId, MapChoice[] Choices);

public sealed class MapSelectionInbox
{
    private readonly HashSet<string> handled = new(StringComparer.Ordinal);

    public MapSelectionRequest? Claim(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        string id = document.RootElement.GetProperty("request_id").GetString()!;
        if (!Guid.TryParse(id, out _)) throw new JsonException("Invalid map request ID.");
        var choices = document.RootElement.GetProperty("choices").EnumerateArray()
            .Select(item => new MapChoice(item.GetProperty("id").GetString()!,
                item.GetProperty("display_name").GetString()!)).ToArray();
        return handled.Add(id) ? new(id, choices) : null;
    }

    public static string Response(MapSelectionRequest request, MapChoice choice) =>
        JsonSerializer.Serialize(new { request_id = request.RequestId, map_id = choice.Id });
}
