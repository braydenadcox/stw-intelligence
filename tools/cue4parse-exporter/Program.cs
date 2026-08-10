using System.Text.Json;
using System.Text.Json.Serialization;
using CUE4Parse.Compression;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.FileProvider.Objects;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json;

internal static class Program
{
    private const int ResultSampleLimit = 50;

    public static async Task<int> Main(string[] args)
    {
        try
        {
            var options = Options.Parse(args);
            var settings = FModelSettings.Load(options.FModelSettingsPath);
            var mappingPath = options.MappingPath ?? settings.FindLatestMapping();
            var scopes = ExportManifest.Load(options.ManifestPath).Scopes
                .Select(Scope.Normalize)
                .Distinct()
                .ToArray();
            if (scopes.Length == 0)
                throw new ArgumentException("manifest contains no export scopes");

            await InitializeCompression(settings);
            var provider = new DefaultFileProvider(
                settings.GameDirectory,
                SearchOption.TopDirectoryOnly,
                new VersionContainer((EGame)settings.UeVersion),
                StringComparer.OrdinalIgnoreCase)
            {
                MappingsContainer = new FileUsmapTypeMappingsProvider(
                    mappingPath, StringComparer.OrdinalIgnoreCase)
            };
            provider.Initialize();
            await provider.MountAsync();
            await provider.SubmitKeysAsync(settings.ReadAesKeys());
            provider.PostMount();
            provider.LoadVirtualPaths();

            var packageFiles = provider.Files.Values
                .Where(file => file.IsUePackage)
                .GroupBy(file => file.PathWithoutExtension, StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First())
                .OrderBy(file => file.PathWithoutExtension, StringComparer.OrdinalIgnoreCase)
                .ToArray();
            var matches = packageFiles
                .Where(file => scopes.Any(scope => scope.Matches(file.PathWithoutExtension)))
                .Where(file => options.Contains is null ||
                    file.PathWithoutExtension.Contains(options.Contains, StringComparison.OrdinalIgnoreCase))
                .ToArray();
            var unmatched = scopes
                .Where(scope => !packageFiles.Any(file => scope.Matches(file.PathWithoutExtension)))
                .Select(scope => new { scope.Kind, scope.Path })
                .ToArray();
            var scopeSummary = BuildScopeSummary(scopes, packageFiles);

            if (matches.Length > options.MaxPackages)
            {
                WriteResult(new
                {
                    status = "refused",
                    reason = "package_limit_exceeded",
                    matched_package_count = matches.Length,
                    max_packages = options.MaxPackages,
                    unmatched_scopes = unmatched,
                    scope_summary = scopeSummary,
                    sample = matches.Take(ResultSampleLimit).Select(file => file.PathWithoutExtension)
                });
                return 3;
            }

            if (options.DryRun)
            {
                WriteResult(new
                {
                    status = "dry_run",
                    available_package_count = packageFiles.Length,
                    matched_package_count = matches.Length,
                    max_packages = options.MaxPackages,
                    mapping_path = mappingPath,
                    output_root = options.OutputDirectory,
                    unmatched_scopes = unmatched,
                    scope_summary = scopeSummary,
                    sample = matches.Take(ResultSampleLimit).Select(file => file.PathWithoutExtension)
                });
                return unmatched.Length == scopes.Length ? 4 : 0;
            }

            Directory.CreateDirectory(options.OutputDirectory);
            var exported = new List<string>();
            var failures = new List<object>();
            foreach (var file in matches)
            {
                try
                {
                    var exports = provider.LoadPackage(file).GetExports();
                    var json = JsonConvert.SerializeObject(exports, Formatting.Indented);
                    var destination = Path.Combine(
                        options.OutputDirectory,
                        file.PathWithoutExtension.Replace('/', Path.DirectorySeparatorChar) + ".json");
                    Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                    var temporary = destination + $".{Environment.ProcessId}.tmp";
                    await File.WriteAllTextAsync(temporary, json);
                    File.Move(temporary, destination, true);
                    exported.Add(file.PathWithoutExtension);
                }
                catch (Exception exception)
                {
                    failures.Add(new
                    {
                        package_path = file.PathWithoutExtension,
                        error_type = exception.GetType().Name,
                        message = exception.Message
                    });
                }
            }

            WriteResult(new
            {
                status = failures.Count == 0 ? "exported" : "partial",
                matched_package_count = matches.Length,
                exported_package_count = exported.Count,
                failed_package_count = failures.Count,
                output_root = options.OutputDirectory,
                unmatched_scopes = unmatched,
                failures,
                sample = exported.Take(ResultSampleLimit)
            });
            return failures.Count == 0 ? 0 : 5;
        }
        catch (Exception exception)
        {
            WriteResult(new
            {
                status = "error",
                error_type = exception.GetType().Name,
                message = exception.Message
            });
            return 2;
        }
    }

    private static async Task InitializeCompression(FModelSettings settings)
    {
        var dataDirectory = Path.Combine(settings.OutputDirectory, ".data");
        var oodlePath = Path.Combine(dataDirectory, OodleHelper.OODLE_NAME_CURRENT);
        var zlibPath = Path.Combine(dataDirectory, ZlibHelper.DLL_NAME);
        if (!File.Exists(oodlePath))
            throw new FileNotFoundException("FModel Oodle library was not found", oodlePath);
        if (!File.Exists(zlibPath))
            throw new FileNotFoundException("FModel zlib library was not found", zlibPath);
        await OodleHelper.InitializeAsync(oodlePath);
        await ZlibHelper.InitializeAsync(zlibPath);
    }

    private static void WriteResult(object value) =>
        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(
            value, new JsonSerializerOptions { WriteIndented = true }));

    private static object[] BuildScopeSummary(Scope[] scopes, GameFile[] packageFiles) =>
        scopes.Where(scope => scope.Kind == "folder").Select(scope =>
        {
            var matching = packageFiles
                .Select(file => file.PathWithoutExtension)
                .Where(scope.Matches)
                .ToArray();
            var prefixLength = scope.Path.Length + 1;
            var childGroups = scope.Kind == "folder"
                ? matching
                    .Where(path => path.Length > prefixLength)
                    .Select(path => path[prefixLength..].Split('/')[0])
                    .GroupBy(child => child, StringComparer.OrdinalIgnoreCase)
                    .Select(group => new { name = group.Key, package_count = group.Count() })
                    .OrderByDescending(group => group.package_count)
                    .ThenBy(group => group.name, StringComparer.OrdinalIgnoreCase)
                    .Take(100)
                    .ToArray()
                : [];
            return (object)new
            {
                kind = scope.Kind,
                path = scope.Path,
                package_count = matching.Length,
                immediate_children = childGroups
            };
        }).ToArray();
}

internal sealed record Options(
    string FModelSettingsPath,
    string ManifestPath,
    string OutputDirectory,
    string? MappingPath,
    int MaxPackages,
    bool DryRun,
    string? Contains)
{
    public static Options Parse(string[] args)
    {
        string? settings = null;
        string? manifest = null;
        string? output = null;
        string? mapping = null;
        var maxPackages = 5000;
        var dryRun = false;
        string? contains = null;
        for (var index = 0; index < args.Length; index++)
        {
            switch (args[index])
            {
                case "--fmodel-settings": settings = Next(args, ref index); break;
                case "--manifest": manifest = Next(args, ref index); break;
                case "--output": output = Next(args, ref index); break;
                case "--mapping": mapping = Next(args, ref index); break;
                case "--max-packages": maxPackages = int.Parse(Next(args, ref index)); break;
                case "--dry-run": dryRun = true; break;
                case "--contains": contains = Next(args, ref index); break;
                default: throw new ArgumentException($"unknown argument: {args[index]}");
            }
        }
        if (maxPackages < 1)
            throw new ArgumentOutOfRangeException(nameof(maxPackages));
        return new Options(
            Path.GetFullPath(settings ?? throw new ArgumentException("--fmodel-settings is required")),
            Path.GetFullPath(manifest ?? throw new ArgumentException("--manifest is required")),
            Path.GetFullPath(output ?? throw new ArgumentException("--output is required")),
            mapping is null ? null : Path.GetFullPath(mapping),
            maxPackages,
            dryRun,
            contains);
    }

    private static string Next(string[] args, ref int index)
    {
        if (++index >= args.Length)
            throw new ArgumentException($"missing value after {args[index - 1]}");
        return args[index];
    }
}

internal sealed record Scope(string Path, string Kind)
{
    private static readonly string StwRoot =
        "FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/";
    private static readonly string[] ApprovedSharedRoots =
    [
        "FortniteGame/Content/Abilities/",
        "FortniteGame/Content/Balance/",
        "FortniteGame/Content/GameplayEffectTemplates/",
        "FortniteGame/Content/Items/"
    ];

    public static Scope Normalize(Scope input)
    {
        var path = input.Path.Trim().Replace('\\', '/').TrimEnd('/');
        if (path.StartsWith("/SaveTheWorld/", StringComparison.OrdinalIgnoreCase))
            path = StwRoot + path["/SaveTheWorld/".Length..];
        else if (path.StartsWith("/Game/", StringComparison.OrdinalIgnoreCase))
            path = "FortniteGame/Content/" + path["/Game/".Length..];
        path = path.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase)
            ? path[..^".uasset".Length]
            : path;
        var kind = input.Kind.Trim().ToLowerInvariant();
        if (kind is not ("package" or "folder"))
            throw new ArgumentException($"unsupported scope kind: {input.Kind}");
        var exactPackageIsSafe = kind == "package" &&
            path.StartsWith("FortniteGame/", StringComparison.OrdinalIgnoreCase);
        var folderIsApproved = path.Equals(StwRoot.TrimEnd('/'), StringComparison.OrdinalIgnoreCase) ||
            path.StartsWith(StwRoot, StringComparison.OrdinalIgnoreCase) ||
            ApprovedSharedRoots.Any(root => path.StartsWith(root, StringComparison.OrdinalIgnoreCase));
        if (!exactPackageIsSafe && !folderIsApproved)
            throw new ArgumentException($"scope is outside approved STW/shared roots: {input.Path}");
        return new Scope(path, kind);
    }

    public bool Matches(string packagePath) => Kind == "package"
        ? packagePath.Equals(Path, StringComparison.OrdinalIgnoreCase)
        : packagePath.StartsWith(Path + "/", StringComparison.OrdinalIgnoreCase) ||
          packagePath.Equals(Path, StringComparison.OrdinalIgnoreCase);
}

internal sealed record ExportManifest(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    Scope[] Scopes)
{
    public static ExportManifest Load(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException("export manifest was not found", path);
        var manifest = System.Text.Json.JsonSerializer.Deserialize<ExportManifest>(
            File.ReadAllText(path), new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
        if (manifest is null || manifest.SchemaVersion != 1)
            throw new InvalidDataException("export manifest schema_version must be 1");
        return manifest;
    }
}

internal sealed record FModelSettings(
    string OutputDirectory,
    string GameDirectory,
    uint UeVersion,
    string MainKey,
    DynamicKey[] DynamicKeys)
{
    public static FModelSettings Load(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException("FModel settings were not found", path);
        using var document = JsonDocument.Parse(File.ReadAllText(path));
        var root = document.RootElement;
        var output = root.GetProperty("OutputDirectory").GetString()
            ?? throw new InvalidDataException("FModel OutputDirectory is missing");
        var perDirectory = root.GetProperty("PerDirectory");
        var configuredGameDirectory = root.TryGetProperty("GameDirectory", out var configured)
            ? configured.GetString()
            : null;
        var game = perDirectory.EnumerateObject()
            .Select(entry => entry.Value)
            .FirstOrDefault(candidate =>
                candidate.ValueKind == JsonValueKind.Object &&
                candidate.TryGetProperty("GameDirectory", out var directory) &&
                string.Equals(directory.GetString(), configuredGameDirectory,
                    StringComparison.OrdinalIgnoreCase));
        if (game.ValueKind != JsonValueKind.Object)
            game = perDirectory.EnumerateObject().FirstOrDefault().Value;
        if (game.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("FModel game configuration is missing");
        var gameDirectory = game.GetProperty("GameDirectory").GetString()
            ?? throw new InvalidDataException("FModel GameDirectory is missing");
        var ueVersion = game.GetProperty("UeVersion").GetUInt32();
        var aes = game.GetProperty("AesKeys");
        var mainKey = aes.GetProperty("mainKey").GetString()
            ?? throw new InvalidDataException("FModel main AES key is missing");
        var dynamicKeys = aes.GetProperty("dynamicKeys")
            .EnumerateArray()
            .Select(item => new DynamicKey(
                item.GetProperty("guid").GetString() ?? "",
                item.GetProperty("key").GetString() ?? ""))
            .ToArray();
        return new FModelSettings(output, gameDirectory, ueVersion, mainKey, dynamicKeys);
    }

    public string FindLatestMapping()
    {
        var mappingDirectory = new DirectoryInfo(Path.Combine(OutputDirectory, ".data", "mappings"));
        var mapping = mappingDirectory.Exists
            ? mappingDirectory.GetFiles("*.usmap").OrderByDescending(file => file.LastWriteTimeUtc).FirstOrDefault()
            : null;
        return mapping?.FullName
            ?? throw new FileNotFoundException("no FModel .usmap mapping file was found");
    }

    public IEnumerable<KeyValuePair<FGuid, FAesKey>> ReadAesKeys()
    {
        yield return new KeyValuePair<FGuid, FAesKey>(new FGuid(), new FAesKey(MainKey));
        foreach (var dynamicKey in DynamicKeys)
        {
            var guid = dynamicKey.Guid.Replace("-", "").Replace("{", "").Replace("}", "");
            if (guid.Length == 32 && !string.IsNullOrWhiteSpace(dynamicKey.Key))
                yield return new KeyValuePair<FGuid, FAesKey>(new FGuid(guid), new FAesKey(dynamicKey.Key));
        }
    }
}

internal sealed record DynamicKey(string Guid, string Key);
