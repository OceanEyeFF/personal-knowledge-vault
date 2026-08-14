<# Safe, fail-closed workspace removal helpers for internal package smoke. #>

#requires -Version 5.1

Set-StrictMode -Version Latest

function Test-InternalPathContainedBy {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $trimChars = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $candidatePath = ([System.IO.Path]::GetFullPath($Candidate)).TrimEnd($trimChars)
    $rootPath = ([System.IO.Path]::GetFullPath($Root)).TrimEnd($trimChars)
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-InternalWorkspaceTreeSafeForRemoval {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ForbiddenRoot,
        [Parameter(Mandatory = $true)][string]$RequiredLeafPrefix
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (Test-InternalPathContainedBy -Candidate $resolved -Root $ForbiddenRoot) {
        throw "refusing to remove an internal workspace below the forbidden root: $resolved"
    }
    $leaf = [System.IO.Path]::GetFileName($resolved.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ))
    if (-not $leaf.StartsWith($RequiredLeafPrefix, [System.StringComparison]::Ordinal)) {
        throw "refusing to remove an internal workspace with an unexpected name: $resolved"
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "internal workspace is missing or not a directory: $resolved"
    }

    $rootItem = Get-Item -LiteralPath $resolved -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "internal workspace root is a reparse point: $resolved"
    }
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($resolved)
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            $candidate = [System.IO.Path]::GetFullPath($item.FullName)
            if (-not (Test-InternalPathContainedBy -Candidate $candidate -Root $resolved)) {
                throw "internal workspace traversal escaped its root: $candidate"
            }
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "internal workspace contains a reparse point: $candidate"
            }
            if ($item.PSIsContainer) {
                $stack.Push($candidate)
            }
        }
    }

    # Re-resolve the root immediately before the caller removes it.  A changed
    # root or newly introduced reparse point fails closed and leaves evidence.
    $rootAfter = Get-Item -LiteralPath $resolved -Force -ErrorAction Stop
    if (($rootAfter.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        -not ([System.IO.Path]::GetFullPath($rootAfter.FullName)).Equals(
            $resolved,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "internal workspace root changed during removal validation: $resolved"
    }
    return $resolved
}

function Remove-InternalWorkspaceSafely {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ForbiddenRoot,
        [Parameter(Mandatory = $true)][string]$RequiredLeafPrefix
    )

    $target = Assert-InternalWorkspaceTreeSafeForRemoval -Path $Path `
        -ForbiddenRoot $ForbiddenRoot -RequiredLeafPrefix $RequiredLeafPrefix
    # A second full walk closes the normal validation/use window after any
    # error handling and before the destructive operation.
    $target = Assert-InternalWorkspaceTreeSafeForRemoval -Path $target `
        -ForbiddenRoot $ForbiddenRoot -RequiredLeafPrefix $RequiredLeafPrefix

    # Never delegate traversal to Remove-Item -Recurse.  The packaged child ran
    # inside this tree and could have left a junction/symlink behind after the
    # first audit.  Delete leaf-first without following reparse points, checking
    # every exact item immediately before its non-recursive delete.  A newly
    # introduced child makes Directory.Delete fail closed and preserves the
    # workspace for inspection.
    $stack = [System.Collections.Generic.Stack[object]]::new()
    $stack.Push([pscustomobject]@{ Path = $target; Expanded = $false })
    while ($stack.Count -gt 0) {
        $frame = $stack.Pop()
        $candidate = [System.IO.Path]::GetFullPath([string]$frame.Path)
        if (-not (Test-InternalPathContainedBy -Candidate $candidate -Root $target)) {
            throw "internal workspace deletion escaped its exact root: $candidate"
        }
        $item = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "internal workspace changed to a reparse point during deletion: $candidate"
        }

        if (-not $item.PSIsContainer) {
            [System.IO.File]::Delete($candidate)
            continue
        }
        if (-not [bool]$frame.Expanded) {
            $stack.Push([pscustomobject]@{ Path = $candidate; Expanded = $true })
            foreach ($child in @(Get-ChildItem -LiteralPath $candidate -Force -ErrorAction Stop)) {
                $childPath = [System.IO.Path]::GetFullPath($child.FullName)
                if (-not (Test-InternalPathContainedBy -Candidate $childPath -Root $target)) {
                    throw "internal workspace deletion traversal escaped its root: $childPath"
                }
                if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "internal workspace contains a reparse point during deletion: $childPath"
                }
                $stack.Push([pscustomobject]@{ Path = $childPath; Expanded = $false })
            }
            continue
        }

        $current = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
        if (($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not $current.PSIsContainer) {
            throw "internal workspace directory identity changed during deletion: $candidate"
        }
        if (@(Get-ChildItem -LiteralPath $candidate -Force -ErrorAction Stop).Count -ne 0) {
            throw "internal workspace directory changed during deletion: $candidate"
        }
        [System.IO.Directory]::Delete($candidate, $false)
    }
    if (Test-Path -LiteralPath $target) {
        throw "internal workspace cleanup did not remove its exact target: $target"
    }
}
