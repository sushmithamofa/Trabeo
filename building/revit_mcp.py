r"""Build in Revit through a live connection: the Revit MCP plug-in.

The plug-in (mcp-servers-for-revit, in Revit's Addins folder) listens on a local port
(its mcp-port.txt, 8080 by default) for one JSON-RPC message per line. Its
send_code_to_revit command runs C# inside Revit, in Revit's API context. The C# here
starts IronPython (the one Rhino 8 installs) and runs exactly the scripts that are run
by hand otherwise - revit\build_model.py on each floor's model.json, bottom floor first,
then revit\build_extras.py on foundations.json. Nothing about building is duplicated.

Two ways:
  build(run, ...)                  into the model open in Revit
  build(run, new_file=path, ...)   into a new project from Revit's default template,
                                   saved as a .rvt for the user to download

Revit has to be running with the MCP service switched on, and a project open (any,
for a new file). A long floor can outlast the plug-in's own wait: every step also
writes its result to a file, and that file is waited for instead.
"""
from __future__ import annotations

import glob
import importlib.util
import io
import json
import os
import re
import socket
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIT_DIR = os.path.join(ROOT, "revit")


class RevitError(RuntimeError):
    pass


# ---------------------------------------------------------------- the connection
def plugin_port(default=8080):
    """The plug-in's port, from its mcp-port.txt (newest Revit version first)."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Autodesk", "Revit", "Addins")
    for f in sorted(glob.glob(os.path.join(base, "*", "revit_mcp_plugin", "mcp-port.txt")), reverse=True):
        try:
            with open(f, encoding="utf-8") as h:
                return int(h.read().strip())
        except (OSError, ValueError):
            continue
    return default


def call(method, params, timeout=60.0, port=None, host="127.0.0.1"):
    """One JSON-RPC request to the plug-in; returns its result or raises RevitError."""
    rid = str(uuid.uuid4())
    msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid}) + "\n"
    try:
        s = socket.create_connection((host, port or plugin_port()), timeout=5)
    except OSError:
        raise RevitError("NOT_CONNECTED")
    try:
        s.settimeout(timeout)
        s.sendall(msg.encode("utf-8"))
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                raise RevitError("TIMEOUT")
            if not chunk:
                raise RevitError("Revit closed the connection.")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                r = json.loads(line.decode("utf-8"))
                if r.get("id") not in (rid, None):
                    continue
                if r.get("error"):
                    e = r["error"]
                    raise RevitError(e.get("message") if isinstance(e, dict) else str(e))
                return r.get("result")
    finally:
        s.close()


def _unwrap(res):
    """send_code_to_revit's answer, whatever envelope this plug-in version uses."""
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            return res
    if isinstance(res, dict):
        if res.get("success") is False or res.get("Success") is False:
            raise RevitError(res.get("errorMessage") or res.get("ErrorMessage") or res.get("message")
                             or json.dumps(res))
        for k in ("result", "Result", "data", "output"):
            if k in res:
                return _unwrap(res[k])
    return res


def run_code(code, parameters=(), mode="none", timeout=60.0, port=None):
    return _unwrap(call("send_code_to_revit", {"code": code, "parameters": [str(p) for p in parameters],
                                               "transactionMode": mode}, timeout=timeout, port=port))


STATUS_CS = r"""
if (document == null) return "NODOC";
return "OK|" + document.Application.VersionNumber + "|" + document.Title;
"""


def status(port=None):
    """{connected, version, document, message} - what the Export page shows."""
    try:
        r = str(run_code(STATUS_CS, timeout=8, port=port) or "")
    except RevitError as exc:
        if str(exc) == "NOT_CONNECTED":
            return {"connected": False, "message": "Revit is not connected. Open Revit and switch on the MCP "
                                                   "service (Add-Ins tab), then check again."}
        if str(exc) == "TIMEOUT":
            return {"connected": False, "message": "Revit is busy. Finish what it is doing, then check again."}
        return {"connected": False, "message": "Revit answered with an error: %s" % exc}
    if r.startswith("OK|"):
        _, ver, title = (r.split("|", 2) + ["", ""])[:3]
        return {"connected": True, "version": ver, "document": title,
                "message": "Connected to Revit %s - %s" % (ver, title)}
    if r == "NODOC":
        return {"connected": False, "message": "Revit is connected but no project is open. Open a project "
                                               "(any, for a new file) and check again."}
    # an answer meant for another request: the plug-in mixes replies up while Revit is
    # still running something else (seen when a build was already under way)
    return {"connected": False, "message": "Revit is busy with another task. Wait for it to finish, then check again."}


# ---------------------------------------------------------------- IronPython for Revit
BUNDLED_IPY = os.path.join(REVIT_DIR, "ironpython3")      # IronPython 3.4.2 (net8.0), shipped with the app


def ironpython(revit_version):
    """(dll folder, standard library) of the IronPython to run the scripts with.
    Revit 2025 and later (.NET 8, here even .NET 10): the IronPython 3.4 shipped with the
    app. Older Revit (.NET Framework 4.8): the IronPython 2.7 that Rhino 7/8 installs."""
    if str(revit_version or "") >= "2025":
        if os.path.exists(os.path.join(BUNDLED_IPY, "IronPython.dll")):
            return BUNDLED_IPY, os.path.join(BUNDLED_IPY, "Lib")
        return None, None
    for rh in ("Rhino 8", "Rhino 7"):
        base = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), rh)
        d = os.path.join(base, "System")
        lib = os.path.join(base, "Plug-ins", "IronPython", "Lib")
        if os.path.exists(os.path.join(d, "IronPython.dll")) and os.path.exists(os.path.join(d, "Microsoft.Scripting.dll")):
            return d, lib if os.path.isdir(lib) else ""
    return None, None


def _run_cs_for(ipy_dir):
    return RUN_CS if os.path.normcase(os.path.abspath(ipy_dir)) == os.path.normcase(BUNDLED_IPY) else RUN_FX_CS


# Runs one of the revit\ scripts, in IronPython, in the Revit document given by path
# (or the active one). C# 5 only: the plug-in's compiler version is not known.
RUN_CS = r"""
string ipDir = Convert.ToString(parameters[0]);
string libDir = Convert.ToString(parameters[1]);
string script = Convert.ToString(parameters[2]);
string inputs = Convert.ToString(parameters[3]);
string target = Convert.ToString(parameters[4]);
string resultPath = Convert.ToString(parameters[5]);
string result;
System.ResolveEventHandler resolve = null;
// the hosting API has generic twins of most methods: pick the plain one exactly
Func<System.Type, string, System.Type[], System.Reflection.MethodInfo> find = (t, n, ps) => t.GetMethods().First(
    m => m.Name == n && !m.IsGenericMethodDefinition && m.GetParameters().Select(p => p.ParameterType).SequenceEqual(ps));
object engine = null;
System.Type et = null;
try {
    Document doc = document;
    if (!string.IsNullOrEmpty(target)) {
        doc = null;
        foreach (Document d in document.Application.Documents)
            if (string.Equals(d.PathName, target, StringComparison.OrdinalIgnoreCase)) doc = d;
        if (doc == null) throw new Exception("The new Revit file is no longer open: " + target);
    }
    System.Reflection.Assembly ipy = null;
/*LOAD*/
    engine = ipy.GetType("IronPython.Hosting.Python").GetMethod("CreateEngine", System.Type.EmptyTypes).Invoke(null, null);
    et = engine.GetType();
    List<string> paths = new List<string>((ICollection<string>)et.GetMethod("GetSearchPaths").Invoke(engine, null));
    if (libDir.Length > 0) paths.Add(libDir);
    paths.Add(System.IO.Path.GetDirectoryName(script));
    et.GetMethod("SetSearchPaths").Invoke(engine, new object[] { paths });
    object scope = et.GetMethod("CreateScope", System.Type.EmptyTypes).Invoke(engine, null);
    System.Type st = scope.GetType();
    System.Reflection.MethodInfo setVar = st.GetMethod("SetVariable", new System.Type[] { typeof(string), typeof(object) });
    setVar.Invoke(scope, new object[] { "__c2r_doc__", doc });
    setVar.Invoke(scope, new object[] { "__name__", "__main__" });
    setVar.Invoke(scope, new object[] { "__file__", script });
    foreach (string line in inputs.Split('\n')) {
        int eq = line.IndexOf('=');
        if (eq > 0) setVar.Invoke(scope, new object[] { line.Substring(0, eq), line.Substring(eq + 1) });
    }
    System.Reflection.MethodInfo exec = find(et, "Execute", new System.Type[] { typeof(string), st });
    exec.Invoke(engine, new object[] {
        "class _C2RUI(object):\n    def __init__(self, d):\n        self.Document = d\n" +
        "class _C2RApp(object):\n    def __init__(self, d):\n        self.ActiveUIDocument = _C2RUI(d)\n" +
        "__revit__ = _C2RApp(__c2r_doc__)\n", scope });
    // on the script's own first line (a docstring or comment there), so error line numbers stay true
    string code = "from __future__ import division; " + System.IO.File.ReadAllText(script, System.Text.Encoding.UTF8);
    System.Type kindT = et.Assembly.GetType("Microsoft.Scripting.SourceCodeKind");
    object src = et.GetMethod("CreateScriptSourceFromString", new System.Type[] { typeof(string), typeof(string), kindT })
        .Invoke(engine, new object[] { code, script, System.Enum.Parse(kindT, "File") });
    find(src.GetType(), "Execute", new System.Type[] { st }).Invoke(src, new object[] { scope });
    object outv = null;
    if ((bool)find(st, "ContainsVariable", new System.Type[] { typeof(string) }).Invoke(scope, new object[] { "OUT" }))
        outv = find(st, "GetVariable", new System.Type[] { typeof(string) }).Invoke(scope, new object[] { "OUT" });
    result = "OK\n" + Convert.ToString(outv);
} catch (Exception ex) {
    Exception inner = ex;
    while (inner is System.Reflection.TargetInvocationException && inner.InnerException != null) inner = inner.InnerException;
    string msg = inner.ToString();
    try {
        System.Type eoT = et.Assembly.GetType("Microsoft.Scripting.Hosting.ExceptionOperations");
        object eo = et.GetMethods().First(m => m.Name == "GetService" && m.IsGenericMethodDefinition)
            .MakeGenericMethod(eoT).Invoke(engine, new object[] { new object[0] });
        string py = (string)find(eoT, "FormatException", new System.Type[] { typeof(Exception) }).Invoke(eo, new object[] { inner });
        msg = py.Contains(inner.Message) ? py : py + "\n" + inner.GetType().Name + ": " + inner.Message;
    } catch (Exception) { }
    result = "ERROR\n" + msg;
} finally {
    if (resolve != null) System.AppDomain.CurrentDomain.AssemblyResolve -= resolve;
}
try { if (resultPath.Length > 0) System.IO.File.WriteAllText(resultPath, result); } catch (Exception) { }
return result;
"""

# .NET (Revit 2025 and later): IronPython 3.4 from revit\ironpython3, in a load context of
# its own, so it never meets another IronPython already in Revit (Rhino's 2.7, which on
# Revit's .NET 10 cannot even define a class: "Type doesn't have a method with a given name").
_LOAD_CORE = r"""
    string alcKey = "c2r.ipy:" + ipDir.ToLower();
    System.Runtime.Loader.AssemblyLoadContext alc = AppDomain.CurrentDomain.GetData(alcKey) as System.Runtime.Loader.AssemblyLoadContext;
    if (alc == null) {
        alc = new System.Runtime.Loader.AssemblyLoadContext("CAD to Revit - IronPython", false);
        string dir = ipDir;
        alc.Resolving += (ctx, an) => {
            string p = System.IO.Path.Combine(dir, an.Name + ".dll");
            return System.IO.File.Exists(p) ? ctx.LoadFromAssemblyPath(p) : null;
        };
        foreach (string n in new string[] { "Microsoft.Scripting", "Microsoft.Dynamic", "IronPython", "IronPython.Modules" })
            alc.LoadFromAssemblyPath(System.IO.Path.Combine(ipDir, n + ".dll"));
        AppDomain.CurrentDomain.SetData(alcKey, alc);
    }
    ipy = alc.Assemblies.First(a => a.GetName().Name == "IronPython");
"""

# .NET Framework (Revit 2024 and older): the IronPython 2.7 that Rhino installs.
_LOAD_FX = r"""
    string dirFx = ipDir;
    resolve = (s, e) => {
        string p = System.IO.Path.Combine(dirFx, new System.Reflection.AssemblyName(e.Name).Name + ".dll");
        return System.IO.File.Exists(p) ? System.Reflection.Assembly.LoadFrom(p) : null;
    };
    System.AppDomain.CurrentDomain.AssemblyResolve += resolve;
    ipy = System.Reflection.Assembly.LoadFrom(System.IO.Path.Combine(ipDir, "IronPython.dll"));
    System.Reflection.Assembly.LoadFrom(System.IO.Path.Combine(ipDir, "IronPython.Modules.dll"));
"""
RUN_FX_CS = RUN_CS.replace("/*LOAD*/", _LOAD_FX)
RUN_CS = RUN_CS.replace("/*LOAD*/", _LOAD_CORE)

# The levels the floors hang from. In a new file a template level standing where one is
# wanted is renamed; in the user's own model their levels are never renamed or moved.
LEVELS_CS = r"""
string target = Convert.ToString(parameters[0]);
bool fresh = Convert.ToString(parameters[1]) == "new";
Document doc = document;
if (!string.IsNullOrEmpty(target)) {
    doc = null;
    foreach (Document d in document.Application.Documents)
        if (string.Equals(d.PathName, target, StringComparison.OrdinalIgnoreCase)) doc = d;
    if (doc == null) return "ERROR\nThe new Revit file is no longer open: " + target;
}
List<string[]> wanted = new List<string[]>();
for (int i = 2; i < parameters.Length; i++) wanted.Add(Convert.ToString(parameters[i]).Split('|'));
HashSet<string> names = new HashSet<string>(wanted.Select(w => w[0].Trim().ToUpper()));
List<string> report = new List<string>();
using (Transaction t = new Transaction(doc, "CAD to Revit - levels")) {
    t.Start();
    foreach (string[] w in wanted) {
        string name = w[0].Trim();
        double mm = double.Parse(w[1], System.Globalization.CultureInfo.InvariantCulture);
        double z = UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters);
        List<Level> levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().ToList();
        if (levels.Any(l => l.Name.Trim().ToUpper() == name.ToUpper())) { report.Add("have|" + name); continue; }
        Level at = levels.FirstOrDefault(l => Math.Abs(l.Elevation - z) < 0.004);
        if (at != null) {
            if (fresh && !names.Contains(at.Name.Trim().ToUpper())) {
                string old = at.Name; at.Name = name; report.Add("renamed|" + old + "|" + name);
            } else report.Add("clash|" + name + "|" + at.Name);
            continue;
        }
        Level lv = Level.Create(doc, z);
        lv.Name = name;
        report.Add("created|" + name + "|" + w[1]);
        try {
            ViewFamilyType vft = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                .FirstOrDefault(v => v.ViewFamily == ViewFamily.StructuralPlan);
            if (vft == null) vft = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                .FirstOrDefault(v => v.ViewFamily == ViewFamily.FloorPlan);
            if (vft != null) ViewPlan.Create(doc, vft.Id, lv.Id);
        } catch (Exception) { }
    }
    t.Commit();
}
return "OK\n" + string.Join("\n", report);
"""

# The levels already in the model: name|elevation in mm, one per line.
LEVEL_LIST_CS = r"""
Document doc = document;
List<string> rows = new List<string>();
foreach (Level l in new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>())
    rows.Add(l.Name + "|" + UnitUtils.ConvertFromInternalUnits(l.Elevation, UnitTypeId.Millimeters)
        .ToString("0.#", System.Globalization.CultureInfo.InvariantCulture));
return "OK\n" + string.Join("\n", rows);
"""

# Revit's own rectangular concrete column and beam families, loaded only when the model
# has none that build_model.py would take (the same test as its base_symbol()), so an
# office family already in the model is always the one used.
FAMILIES_CS = r"""
string target = Convert.ToString(parameters[0]);
string colPath = Convert.ToString(parameters[1]);
string beamPath = Convert.ToString(parameters[2]);
Document doc = document;
if (!string.IsNullOrEmpty(target)) {
    doc = null;
    foreach (Document d in document.Application.Documents)
        if (string.Equals(d.PathName, target, StringComparison.OrdinalIgnoreCase)) doc = d;
    if (doc == null) return "ERROR\nThe new Revit file is no longer open: " + target;
}
string[] bad = new string[] { "universal", "wide flange", "hss", "angle", "channel", "hollow", "tee", "pipe", "ub-", "uc-", "w-", "hp-", "steel", "round", "circular" };
Func<string, bool> looksRight = n => {
    n = n.ToLower();
    if (bad.Any(b => n.Contains(b))) return false;
    return n.Contains("concrete") || n.Contains("rectangular") || n.Contains("rect");
};
Func<BuiltInCategory, bool> has = cat => new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
    .Cast<FamilySymbol>().Any(s => looksRight((s.FamilyName ?? "") + " " + (s.Name ?? "")));
// an isolated footing family the build can size: Width, Length and a thickness (build_extras)
string footPath = parameters.Length > 3 ? Convert.ToString(parameters[3]) : "";
Func<bool> hasFooting = () => new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol))
    .OfCategory(BuiltInCategory.OST_StructuralFoundation).Cast<FamilySymbol>()
    .Any(s => s.LookupParameter("Width") != null && s.LookupParameter("Length") != null
              && (s.LookupParameter("Foundation Thickness") != null || s.LookupParameter("Thickness") != null));
List<string> rep = new List<string>();
using (Transaction t = new Transaction(doc, "CAD to Revit - load families")) {
    t.Start();
    List<string[]> wants = new List<string[]> {
        new string[] { "column", colPath }, new string[] { "beam", beamPath } };
    if (footPath.Length > 0) wants.Add(new string[] { "footing", footPath });
    foreach (string[] w in wants) {
        BuiltInCategory cat = w[0] == "column" ? BuiltInCategory.OST_StructuralColumns : BuiltInCategory.OST_StructuralFraming;
        if (w[0] == "footing" ? hasFooting() : has(cat)) { rep.Add("have|" + w[0]); continue; }
        if (string.IsNullOrEmpty(w[1]) || !System.IO.File.Exists(w[1])) { rep.Add("none|" + w[0]); continue; }
        Family f;
        if (doc.LoadFamily(w[1], out f)) rep.Add("loaded|" + w[0] + "|" + f.Name);
        else rep.Add("failed|" + w[0] + "|" + w[1]);
    }
    t.Commit();
}
return "OK\n" + string.Join("\n", rep);
"""

NEW_CS = r"""
string template = Convert.ToString(parameters[0]);
string path = Convert.ToString(parameters[1]);
Autodesk.Revit.ApplicationServices.Application app = document.Application;
foreach (Document d in app.Documents)
    if (string.Equals(d.PathName, path, StringComparison.OrdinalIgnoreCase))
        return "ERROR\nA file with that name is open in Revit: close it, then try again.";
if (string.IsNullOrEmpty(template) || !System.IO.File.Exists(template)) template = app.DefaultProjectTemplate;
if (string.IsNullOrEmpty(template) || !System.IO.File.Exists(template)) {
    string root = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                                         "Autodesk", "RVT " + app.VersionNumber, "Templates");
    template = null;
    if (System.IO.Directory.Exists(root)) {
        string[] all = System.IO.Directory.GetFiles(root, "*.rte", System.IO.SearchOption.AllDirectories);
        template = all.FirstOrDefault(f => System.IO.Path.GetFileName(f).ToLower().Contains("struct"));
        if (template == null) template = all.FirstOrDefault();
    }
}
if (string.IsNullOrEmpty(template)) return "ERROR\nNo Revit project template found. Set one in Revit (File > Options > File Locations).";
Document nd = app.NewProjectDocument(template);
SaveAsOptions o = new SaveAsOptions();
o.OverwriteExistingFile = true;
nd.SaveAs(path, o);
return "OK\n" + template;
"""

CLOSE_CS = r"""
string path = Convert.ToString(parameters[0]);
foreach (Document d in document.Application.Documents)
    if (string.Equals(d.PathName, path, StringComparison.OrdinalIgnoreCase)) {
        d.Save();
        d.Close(false);
        return "OK\nsaved";
    }
return "ERROR\nThe new Revit file is no longer open.";
"""


def _result(text):
    """'OK\\n...' or 'ERROR\\n...' -> the text after it, or RevitError."""
    text = str(text if text is not None else "")
    head, _, body = text.partition("\n")
    if head.strip() == "OK":
        return body
    if head.strip() == "ERROR":
        raise RevitError(body.strip() or "Revit reported an error.")
    return text


def run_script(script, inputs, ipy, target="", port=None, wait_s=3600.0):
    """Run revit\\<script> inside Revit; returns its log (OUT). Waits for the result file
    when the plug-in stops waiting before Revit has finished."""
    res_path = os.path.join(os.environ.get("TEMP", ROOT), "c2r_mcp_%s.txt" % uuid.uuid4().hex[:10])
    params = [ipy[0], ipy[1] or "", script, "\n".join("%s=%s" % kv for kv in inputs.items()), target or "", res_path]
    try:
        try:
            return _result(run_code(_run_cs_for(ipy[0]), params, timeout=wait_s, port=port))
        except RevitError as exc:
            if str(exc) == "NOT_CONNECTED":
                raise RevitError("Revit is not connected.")
            t0 = time.time()                    # the plug-in gave up waiting; Revit carries on
            while time.time() - t0 < wait_s:
                if os.path.exists(res_path):
                    time.sleep(0.3)
                    with io.open(res_path, encoding="utf-8", errors="replace") as f:
                        return _result(f.read())
                time.sleep(2)
            raise RevitError("Revit did not finish within %d minutes (%s)." % (wait_s // 60, exc))
    finally:
        try:
            os.remove(res_path)
        except OSError:
            pass


FAMILY_FILES = {
    "column": ["English/US/Structural Columns/Concrete/M_Concrete-Rectangular-Column.rfa",
               "English/UK/Structural Columns/Concrete/Concrete Rectangular.rfa",
               "English-Imperial/US/Structural Columns/Concrete/Concrete-Rectangular-Column.rfa",
               "*/Structural Columns/Concrete/*Rectangular*.rfa",
               "*/*/Structural Columns/Concrete/*Rectangular*.rfa"],
    "beam": ["English/US/Structural Framing/Concrete/M_Concrete-Rectangular Beam.rfa",
             "English/UK/Structural Framing/Concrete/Concrete - Rectangular Beam.rfa",
             "English-Imperial/US/Structural Framing/Concrete/Concrete-Rectangular Beam.rfa",
             "*/Structural Framing/Concrete/*Rectangular*Beam*.rfa",
             "*/*/Structural Framing/Concrete/*Rectangular*Beam*.rfa"],
    # the isolated footing build_extras sizes (Width, Length, Foundation Thickness)
    "footing": ["English/US/Structural Foundations/M_Footing-Rectangular.rfa",
                "English/UK/Structural Foundations/Footing-Rectangular.rfa",
                "English-Imperial/US/Structural Foundations/Footing-Rectangular.rfa",
                "*/Structural Foundations/*Footing*Rectangular*.rfa",
                "*/*/Structural Foundations/*Footing*Rectangular*.rfa"],
}


def family_file(kind, revit_version, root=None):
    """Revit's own rectangular concrete family of that kind, from its content library
    (metric first), or "" when the library is not installed."""
    root = root or os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Autodesk",
                                "RVT %s" % revit_version, "Libraries")
    for pat in FAMILY_FILES[kind]:
        for f in sorted(glob.glob(os.path.join(root, *pat.split("/")))):
            n = os.path.basename(f).lower()
            if "drop cap" not in n and "precast" not in n:
                return f
    return ""


# ---------------------------------------------------------------- what to build
def _rir():
    spec = importlib.util.spec_from_file_location("rir_cad2revit", os.path.join(REVIT_DIR, "rir_cad2revit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def below_name(name):
    """The level the lowest floor's columns rise from, named in the same scheme:
    'Level 4' -> 'Level 3', 'Ground Floor' -> 'Below Ground Floor'."""
    m = re.match(r"(?i)^(level\s*)(-?\d+)$", name or "")
    return ("%s%d" % (m.group(1), int(m.group(2)) - 1)) if m else "Below %s" % name


def level_targets(run, decisions=None):
    """[(name, elevation_mm or None, source)] for every floor's level, bottom floor first,
    then the level below the lowest floor (its columns rise from there) at the start.

    An elevation comes from the drawings (levels.json, read or decided in the review) or
    from the project's floor-level decisions (decisions["levels"], by Revit name); source
    says which. With neither it is None: nothing is assumed, and build() stops for it
    unless the level already stands in the user's own model."""
    rir = _rir()
    floors = rir.exported_floors(run)
    known, spelt = {}, {}
    lj = os.path.join(run, "revit_build", "levels.json")
    if os.path.exists(lj):
        with io.open(lj, encoding="utf-8") as f:
            for e in json.load(f):
                if e.get("status") in ("read", "override") and e.get("elevation_mm") is not None:
                    known[(e.get("revit_name") or "").strip().upper()] = (
                        float(e["elevation_mm"]), "decided" if e.get("status") == "override" else "drawings")
                    spelt.setdefault((e.get("revit_name") or "").strip().upper(), (e.get("revit_name") or "").strip())
    for n, v in ((decisions or {}).get("levels") or {}).items():
        if v not in (None, ""):
            known[str(n).strip().upper()] = (float(v), "decided")
            spelt[str(n).strip().upper()] = str(n).strip()
    names = []
    for _, _, lvl in floors:                    # bottom floor first (exported_floors' order)
        if lvl and lvl not in names:
            names.append(lvl)
    if names:
        below = below_name(names[0])
        # the nearest decided level below the lowest floor (Foundation -1200 under the Service
        # Floor), as the 3D view and the Revit build take it - not a leftover 'Below ...' lower down
        z0 = known.get(names[0].strip().upper(), (None, ""))[0]
        floor_names = {n.strip().upper() for n in names}
        under = [(z, k) for k, (z, src) in known.items() if z0 is not None and src == "decided"
                 and z < z0 - 1.0 and k and k not in floor_names]
        if under:
            below = spelt.get(max(under)[1], below)
        if below.upper() not in {n.upper() for n in names}:
            names.insert(0, below)
    return [(n,) + known.get(n.strip().upper(), (None, "")) for n in names]


def existing_levels(port=None):
    """{name in capitals: elevation mm} of the levels in the model open in Revit."""
    out = {}
    for line in _result(run_code(LEVEL_LIST_CS, [], timeout=60, port=port)).strip().splitlines():
        name, _, mm = line.rpartition("|")
        if name:
            try:
                out[name.strip().upper()] = float(mm)
            except ValueError:
                pass
    return out


def levels_needed(targets, existing=None):
    """The levels that stop a build: no elevation from the drawings or a decision, and
    (building into the user's own model) not already standing there. A new file's
    template levels never count - their heights are the template's, not the job's."""
    have = {k.upper() for k in (existing or {})}
    return [n for n, z, _ in targets if z is None and n.strip().upper() not in have]


def build(run, new_file=None, template="", floors=None, extras=True, log=print, port=None, decisions=None):
    """Build an exported analysis in Revit: in the open model, or (new_file) in a new
    .rvt saved at that path. Returns the .rvt path or the open document's title.

    decisions: the project decisions (overrides.json "project"); its floor levels are
    used where the drawings give none. A level with neither stops the build before
    anything is made - no storey height is assumed."""
    rir = _rir()
    st = status(port)
    if not st.get("connected"):
        raise RevitError(st["message"])
    ipy = ironpython(st.get("version"))
    if not ipy[0]:
        raise RevitError("The Python that runs inside Revit is missing (revit\\ironpython3 in the app folder; "
                         "for Revit 2024 and older, Rhino 7 or 8). Reinstall the app, or use the "
                         "Rhino.Inside or package options.")
    fl = rir.chosen(rir.exported_floors(run), floors)
    if not fl:
        raise RevitError("This analysis has no floor to build. Export to Revit first.")
    log("REVIT %s" % st["message"])
    lv = level_targets(run, decisions)
    have = {} if new_file else existing_levels(port)
    missing = levels_needed(lv, have)
    if missing:
        raise RevitError("These levels need an elevation first: %s. The drawings give none, so nothing was "
                         "built. Enter them on the Export page under 'Floor levels'%s." % (
                             ", ".join(missing), "" if new_file else " (or create them in your Revit model)"))
    target = ""
    if new_file:
        os.makedirs(os.path.dirname(new_file), exist_ok=True)
        log("REVIT Creating a new Revit project ...")
        tpl = _result(run_code(NEW_CS, [template or "", new_file], timeout=600, port=port))
        log("REVIT Template: %s" % tpl.strip())
        target = new_file
    try:
        wanted = [(n, z, src) for n, z, src in lv if z is not None]
        if wanted:
            rep = _result(run_code(LEVELS_CS, [target, "new" if new_file else "open"]
                                   + ["%s|%.1f" % (n, z) for n, z, _ in wanted], timeout=300, port=port))
            source = {n: src for n, _, src in wanted}
            for line in rep.strip().splitlines():
                p = line.split("|")
                if p[0] == "created":
                    log("REVIT Level '%s' created at %+.0f mm (%s)" % (p[1], float(p[2]),
                        "your decision" if source.get(p[1]) == "decided" else "from the drawings"))
                elif p[0] == "renamed":
                    log("REVIT Level '%s' renamed '%s'" % (p[1], p[2]))
                elif p[0] == "clash":
                    log("REVIT Level '%s' not created: '%s' already stands there" % (p[1], p[2]))
        fam = _result(run_code(FAMILIES_CS, [target, family_file("column", st.get("version")),
                                             family_file("beam", st.get("version")),
                                             # Revit's footing family too, when there are footings to build
                                             family_file("footing", st.get("version")) if rir.foundations_file(run) else ""],
                               timeout=300, port=port))
        for line in fam.strip().splitlines():
            p = line.split("|")
            if p[0] == "loaded":
                log("REVIT Loaded Revit's %s family '%s'" % (p[1], p[2]))
            elif p[0] in ("none", "failed"):
                log("REVIT No rectangular concrete %s family in the model, and Revit's library has none to load" % p[1])
        problems = []
        for i, (label, mj, lvl) in enumerate(fl, 1):
            log("REVIT FLOOR %d of %d: %s" % (i, len(fl), label))
            out = run_script(os.path.join(REVIT_DIR, "build_model.py"),
                             {"model_json": mj, "level_name": lvl or "", "mode": "replace"}, ipy, target, port)
            problems += _report(out, label, log)
        fj = rir.foundations_file(run)
        if extras and fj:
            log("REVIT FLOOR foundations, levels and stairs")
            out = run_script(os.path.join(REVIT_DIR, "build_extras.py"), {"extras_json": fj}, ipy, target, port)
            problems += _report(out, "foundations", log)
    finally:
        if new_file:
            try:
                _result(run_code(CLOSE_CS, [new_file], timeout=600, port=port))
                log("REVIT Saved %s" % new_file)
            except RevitError as exc:
                log("REVIT The new file could not be saved and closed: %s" % exc)
    log("REVIT DONE" if not problems else "REVIT DONE: %d step%s stopped - %s" % (
        len(problems), "" if len(problems) == 1 else "s", problems[0]))
    return new_file or st.get("document")


# what the build scripts print when something is not built: "STOP: ...", "FAILED:" (with its
# traceback), "beam B1 failed: <Revit's message>" for one element, or a line starting "Error"
PROBLEM = re.compile(r"^(?:STOP|FAILED)|(?i:\bfailed\s*:|^error\b)")


def _report(out, label, log):
    """A script's log into the app's details; its STOP / FAILED / '<element> failed:' lines
    also as problems."""
    lines = [x.rstrip() for x in (out or "").splitlines() if x.strip()]
    for x in lines:
        log("  " + x)
    bad = [x.strip() for x in lines if PROBLEM.search(x.strip())]
    if bad:
        log("REVIT PROBLEM %s: %s" % (label, bad[0]))
        return ["%s: %s" % (label, bad[0])]
    return []
