r"""Compile revit\c2r_batch\C2RBatch.cs into C2RBatch.dll (the add-in that makes a .rvt
with no MCP). For the people building the app; the DLL ships with it.

    python tools\build_batch_addin.py [--revit 2025]

Uses the C# compiler (Roslyn) that Rhino 8 installs, run on .NET through pythonnet
(pip install pythonnet), against .NET 8 and that Revit's API - so no .NET SDK is needed.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "revit", "c2r_batch", "C2RBatch.cs")
OUT = os.path.join(ROOT, "revit", "c2r_batch", "C2RBatch.dll")
ROSLYN = r"C:\Program Files\Rhino 8\System"
DOTNET = r"C:\Program Files\dotnet\shared\Microsoft.NETCore.App"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--revit", default="2025")
    a = ap.parse_args(argv)
    from pythonnet import load
    load("coreclr")
    import clr                                                       # noqa: F401
    from System.Reflection import Assembly, AssemblyName
    for n in ("System.Collections.Immutable", "System.Reflection.Metadata", "Microsoft.CodeAnalysis",
              "Microsoft.CodeAnalysis.CSharp"):
        Assembly.LoadFrom(os.path.join(ROSLYN, n + ".dll"))
    clr.AddReference("Microsoft.CodeAnalysis")
    clr.AddReference("Microsoft.CodeAnalysis.CSharp")
    from Microsoft.CodeAnalysis import MetadataReference, OutputKind
    from Microsoft.CodeAnalysis.CSharp import (CSharpCompilation, CSharpCompilationOptions, CSharpParseOptions,
                                               CSharpSyntaxTree, LanguageVersion)
    from System.Collections.Generic import List
    from Microsoft.CodeAnalysis import SyntaxTree, MetadataReference as MR

    net8 = sorted(d for d in glob.glob(os.path.join(DOTNET, "8.0.*")))
    if not net8:
        print("No .NET 8 runtime in " + DOTNET)
        return 1
    revit = os.path.join(os.environ.get("ProgramW6432", r"C:\Program Files"), "Autodesk", "Revit " + a.revit)
    refs = List[MR]()
    for f in glob.glob(os.path.join(net8[-1], "*.dll")):
        try:
            AssemblyName.GetAssemblyName(f)                       # managed assemblies only
        except Exception:
            continue
        refs.Add(MetadataReference.CreateFromFile(f))
    for n in ("RevitAPI.dll", "RevitAPIUI.dll"):
        refs.Add(MetadataReference.CreateFromFile(os.path.join(revit, n)))

    with open(SRC, encoding="utf-8") as f:
        code = f.read()
    trees = List[SyntaxTree]()
    trees.Add(CSharpSyntaxTree.ParseText(code, CSharpParseOptions(LanguageVersion.CSharp10)))
    opts = CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary)
    comp = CSharpCompilation.Create("C2RBatch", trees, refs, opts)
    from System.IO import MemoryStream, File
    ms = MemoryStream()
    res = comp.Emit(ms)
    if res.Success:
        # a Revit that loaded the old DLL keeps it locked; a locked file can still be renamed
        if os.path.exists(OUT):
            try:
                os.remove(OUT)
            except OSError:
                os.replace(OUT, OUT + ".old-%d" % len(glob.glob(OUT + ".old-*")))
        File.WriteAllBytes(OUT, ms.ToArray())
    errors = [d for d in res.Diagnostics if str(d.Severity) == "Error"]
    for d in errors[:30]:
        print(d.ToString())
    if not res.Success:
        print("FAILED")
        return 1
    print("Wrote " + OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
