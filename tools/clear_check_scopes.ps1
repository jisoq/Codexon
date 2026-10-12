param([Parameter(Mandatory)][string]$Evidence)
$ErrorActionPreference = 'Stop'
$roots = @(Get-ChildItem -LiteralPath $Evidence -Filter 'owner-*.json' | ForEach-Object {
    (Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json).root.TrimEnd('\')
})
function Test-OwnedPath([string]$Value) {
    if (!$Value) { return $false }
    foreach ($scope in $roots) {
        if ($Value.Equals($scope, [StringComparison]::OrdinalIgnoreCase) -or
            $Value.StartsWith($scope + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
$tasks = @($folder.GetTasks(1) | Where-Object {
    $marker = $_.Definition.RegistrationInfo.Description
    $prefix = 'CacheMonitor model observer: '
    $_.Name.StartsWith('CacheMonitor-') -and $marker -and $marker.StartsWith($prefix) -and
        (Test-OwnedPath $marker.Substring($prefix.Length))
})
# Open and retain each process handle before recording or terminating it.
# Match an owned executable or a complete quoted/unquoted scope argument.
$snapshot = @(Get-CimInstance Win32_Process)
$ancestors = @{}
$ancestor = $snapshot | Where-Object ProcessId -eq $PID
while ($ancestor -and !$ancestors.ContainsKey([int]$ancestor.ProcessId)) {
    $ancestors[[int]$ancestor.ProcessId] = $true
    $parent = $snapshot | Where-Object ProcessId -eq $ancestor.ParentProcessId
    if ($parent -and $parent.CreationDate -gt $ancestor.CreationDate) { break }
    $ancestor = $parent
}
$ownedRecords = @{}
foreach ($record in @($snapshot | Where-Object {
    $owned = Test-OwnedPath $_.ExecutablePath
    if (!$owned -and $_.CommandLine) {
        foreach ($scope in $roots) {
            $pattern = '(?:^|[\s"=])' + [regex]::Escape($scope) + '(?:[\\"\s]|$)'
            if ($_.CommandLine -match $pattern) { $owned = $true; break }
        }
    }
    $owned -and !$ancestors.ContainsKey([int]$_.ProcessId)
})) { $ownedRecords[[int]$record.ProcessId] = $record }
# Capture the entire external tree, including children with no scope argument.
# Creation order prevents attaching a child to a recycled parent PID.
do {
    $added = $false
    foreach ($record in $snapshot) {
        $parent = $ownedRecords[[int]$record.ParentProcessId]
        if ($parent -and !$ownedRecords.ContainsKey([int]$record.ProcessId) -and
            $record.CreationDate -ge $parent.CreationDate -and !$ancestors.ContainsKey([int]$record.ProcessId)) {
            $ownedRecords[[int]$record.ProcessId] = $record
            $added = $true
        }
    }
} while ($added)
$processes = @($ownedRecords.Values | ForEach-Object {
    $record = $_
    try {
        $process = [Diagnostics.Process]::GetProcessById($record.ProcessId)
        $null = $process.Handle
        # CIM stores microseconds; GetProcessTimes retains 100 ns ticks.
        if ([Math]::Abs($process.StartTime.ToUniversalTime().ToFileTimeUtc() - $record.CreationDate.ToUniversalTime().ToFileTimeUtc()) -lt 10) {
            $process
        } else { $process.Dispose() }
    } catch [ArgumentException] { }
})
$report = @{
    roots = $roots
    excluded_ancestors = @($ancestors.Keys)
    tasks = @($tasks | ForEach-Object { @{ name=$_.Name; running=$_.GetInstances(0).Count } })
    processes = @($processes | ForEach-Object { @{ pid=$_.Id; started=$_.StartTime.ToUniversalTime().ToString('o'); name=$_.ProcessName } })
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Evidence 'external-processes.json') -Encoding utf8
try {
    foreach ($task in $tasks) { $task.Stop(0); $folder.DeleteTask($task.Name, 0) }
    foreach ($process in $processes) {
        if (!$process.HasExited) { $process.Kill() }
    }
    foreach ($process in $processes) {
        if (!$process.WaitForExit(2000)) { throw 'Owned external process did not exit' }
    }
} finally {
    foreach ($process in $processes) { $process.Dispose() }
}
$report | ConvertTo-Json -Depth 5 -Compress
