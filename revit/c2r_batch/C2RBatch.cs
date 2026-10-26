// CAD to Revit - batch builder: makes a new .rvt with no one at Revit and no MCP.
//
// The app starts Revit with the environment variable C2R_JOB pointing at a job file
// (JSON). In any other Revit session this add-in does nothing at all. Once Revit has
// started, it:
//   1. makes a new project from the template and saves it as the job's .rvt;
//   2. creates the job's levels (a template level standing where one is wanted is renamed);
//   3. loads Revit's rectangular concrete column / beam families when the project has none;
//   4. runs each step - revit\build_model.py per floor, revit\build_extras.py - in the
//      IronPython the app ships, exactly as the live connection does;
//   5. saves and closes the file, and writes the result file the app waits for.
// The same C# as building\revit_mcp.py sends over the live connection, compiled here.
// Progress lines go to the job's log file as they happen.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;

namespace CadToRevit
{
    public class Batch : IExternalApplication
    {
        string jobPath;
        bool running;

        public Result OnStartup(UIControlledApplication a)
        {
            jobPath = Environment.GetEnvironmentVariable("C2R_JOB");
            if (string.IsNullOrEmpty(jobPath) || !File.Exists(jobPath)) return Result.Succeeded;
            a.ControlledApplication.ApplicationInitialized += OnInitialized;
            // nobody is at this Revit: answer its dialogs, keep warnings out of the way
            a.DialogBoxShowing += (s, e) => { if (running) try { e.OverrideResult(1); } catch (Exception) { } };
            a.ControlledApplication.FailuresProcessing += OnFailures;
            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication a) => Result.Succeeded;

        // Nobody is at this Revit to answer its error dialog - and answering it the wrong way
        // rolls the whole build back. Warnings are dismissed; errors are resolved the way
        // Revit's own dialog offers by default (as when a person clicks its first button),
        // and each kind is written to the log.
        readonly HashSet<string> told = new HashSet<string>();

        void OnFailures(object sender, FailuresProcessingEventArgs e)
        {
            if (!running) return;
            FailuresAccessor fa = e.GetFailuresAccessor();
            bool resolved = false;
            foreach (FailureMessageAccessor m in fa.GetFailureMessages())
            {
                if (m.GetSeverity() == FailureSeverity.Warning) { fa.DeleteWarning(m); continue; }
                string what = m.GetDescriptionText();
                if (told.Add(what)) Log("  Revit: " + what + " - resolved as Revit suggests");
                if (m.HasResolutions()) { fa.ResolveFailure(m); resolved = true; }
            }
            if (resolved) e.SetProcessingResult(FailureProcessingResult.ProceedWithCommit);
        }

        JsonElement job;
        string logPath = "";

        void Log(string line)
        {
            try { if (logPath.Length > 0) File.AppendAllText(logPath, line + Environment.NewLine); } catch (Exception) { }
        }

        string S(string name) => job.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() : "";

        void OnInitialized(object sender, ApplicationInitializedEventArgs args)
        {
            Application app = (Application)sender;
            string resultPath = "";
            var outcome = new Dictionary<string, object>();
            var steps = new List<Dictionary<string, string>>();
            Document nd = null;
            running = true;
            try
            {
                job = JsonDocument.Parse(File.ReadAllText(jobPath)).RootElement;
                logPath = S("log");
                resultPath = S("result");
                string path = S("out");
                Log("REVIT Revit " + app.VersionNumber + " started for the new file (no live connection needed).");

                // 1. the new project
                string template = NewTemplate(app, S("template"));
                if (template == null) throw new Exception("No Revit project template found. Set one in Revit (File > Options > File Locations).");
                nd = app.NewProjectDocument(template);
                var so = new SaveAsOptions { OverwriteExistingFile = true };
                nd.SaveAs(path, so);
                Log("REVIT Template: " + template);

                // 2. levels
                var wanted = new List<string[]>();
                foreach (JsonElement l in job.GetProperty("levels").EnumerateArray())
                    wanted.Add(new[] { l[0].GetString(), l[1].GetString(), l[2].GetString() });
                foreach (string r in Levels(nd, wanted)) Log(r);

                // 3. families
                foreach (string r in Families(nd, S("column_family"), S("beam_family"), S("footing_family"))) Log(r);

                // 4. the build steps
                foreach (JsonElement st in job.GetProperty("steps").EnumerateArray())
                {
                    string label = st.GetProperty("label").GetString();
                    Log("REVIT FLOOR " + label);
                    var inputs = new Dictionary<string, string>();
                    foreach (JsonProperty p in st.GetProperty("inputs").EnumerateObject()) inputs[p.Name] = p.Value.GetString();
                    string res = RunScript(nd, S("ipy"), S("lib"), st.GetProperty("script").GetString(), inputs);
                    steps.Add(new Dictionary<string, string> { { "label", label }, { "result", res } });
                }

                // 5. what is really in the file, then save and close
                Func<BuiltInCategory, int> count = c => new FilteredElementCollector(nd).OfCategory(c).WhereElementIsNotElementType().GetElementCount();
                int cols = count(BuiltInCategory.OST_StructuralColumns), beams = count(BuiltInCategory.OST_StructuralFraming),
                    floors = count(BuiltInCategory.OST_Floors), walls = count(BuiltInCategory.OST_Walls);
                Log("REVIT In the file: " + cols + " columns, " + beams + " beams and hidden beams, " + floors + " slabs, " + walls + " walls");
                outcome["in_file"] = new Dictionary<string, int> { { "columns", cols }, { "beams", beams }, { "slabs", floors }, { "walls", walls } };
                nd.Save();
                nd.Close(false);
                nd = null;
                Log("REVIT Saved " + path);
                outcome["ok"] = true;
            }
            catch (Exception ex)
            {
                outcome["ok"] = false;
                outcome["error"] = ex.Message;
                Log("REVIT STOPPED: " + ex.Message);
                try { if (nd != null) { nd.Save(); nd.Close(false); } } catch (Exception) { }
            }
            finally
            {
                running = false;
                outcome["steps"] = steps;
                try { if (resultPath.Length > 0) File.WriteAllText(resultPath, JsonSerializer.Serialize(outcome)); } catch (Exception) { }
            }
        }

        // The template the job names, else Revit's structural one (metric first), else Revit's default.
        static string NewTemplate(Application app, string template)
        {
            if (!string.IsNullOrEmpty(template) && File.Exists(template)) return template;
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                                       "Autodesk", "RVT " + app.VersionNumber, "Templates");
            if (Directory.Exists(root))
            {
                // Revit's structural templates are "Structural Analysis-Default..." (its -Up / -Down
                // variants change the view direction): English metric first, then any language
                foreach (string want in new[] { @"English\Structural Analysis-DefaultMetric.rte",
                                                @"English\Structural Analysis-DefaultGBRENU.rte",
                                                @"English-Imperial\Structural Analysis-Default.rte" })
                    if (File.Exists(Path.Combine(root, want))) return Path.Combine(root, want);
                string any = Directory.GetFiles(root, "*.rte", SearchOption.AllDirectories).OrderBy(f => f)
                    .FirstOrDefault(f => { string n = Path.GetFileName(f).ToLower(); return n.Contains("struct") && !n.Contains("-up") && !n.Contains("-down"); });
                if (any != null) return any;
            }
            template = app.DefaultProjectTemplate;
            return !string.IsNullOrEmpty(template) && File.Exists(template) ? template : null;
        }

        // A new file's levels are the job's: [name, mm, source] each. The template's own levels
        // (their names and heights are the template's) are reused bottom up - with their plan
        // views - renamed and moved to the job's; any left over are removed. Nothing hangs
        // from them yet, so nothing moves with them.
        static List<string> Levels(Document doc, List<string[]> wanted)
        {
            var report = new List<string>();
            var ordered = wanted.OrderBy(w => double.Parse(w[1], System.Globalization.CultureInfo.InvariantCulture)).ToList();
            using (var t = new Transaction(doc, "CAD to Revit - levels"))
            {
                t.Start();
                var tmpl = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation).ToList();
                for (int i = 0; i < tmpl.Count; i++) tmpl[i].Name = "C2R template level " + i;   // free the names first
                for (int i = 0; i < ordered.Count; i++)
                {
                    string name = ordered[i][0].Trim();
                    double mm = double.Parse(ordered[i][1], System.Globalization.CultureInfo.InvariantCulture);
                    double z = UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters);
                    string why = ordered[i][2] == "decided" ? "your decision" : "from the drawings";
                    Level lv;
                    if (i < tmpl.Count) lv = tmpl[i];
                    else
                    {
                        lv = Level.Create(doc, z);
                        try
                        {
                            ViewFamilyType vft = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                                .FirstOrDefault(v => v.ViewFamily == ViewFamily.StructuralPlan)
                                ?? new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                                .FirstOrDefault(v => v.ViewFamily == ViewFamily.FloorPlan);
                            if (vft != null) ViewPlan.Create(doc, vft.Id, lv.Id);
                        }
                        catch (Exception) { }
                    }
                    lv.Elevation = z;
                    lv.Name = name;
                    report.Add("REVIT Level '" + name + "' at " + mm.ToString("+0;-0;0") + " mm (" + why + ")");
                }
                for (int i = ordered.Count; i < tmpl.Count; i++)
                {
                    try { doc.Delete(tmpl[i].Id); }
                    catch (Exception) { report.Add("REVIT The template's extra level could not be removed: " + tmpl[i].Name); }
                }
                t.Commit();
            }
            return report;
        }

        // As FAMILIES_CS: Revit's own families, only when the project has no usable one.
        static List<string> Families(Document doc, string colPath, string beamPath, string footPath = "")
        {
            // an isolated footing family the build can size: Width, Length and a thickness (build_extras)
            Func<bool> hasFooting = () => new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol))
                .OfCategory(BuiltInCategory.OST_StructuralFoundation).Cast<FamilySymbol>()
                .Any(s => s.LookupParameter("Width") != null && s.LookupParameter("Length") != null
                          && (s.LookupParameter("Foundation Thickness") != null || s.LookupParameter("Thickness") != null));
            string[] bad = { "universal", "wide flange", "hss", "angle", "channel", "hollow", "tee", "pipe", "ub-", "uc-", "w-", "hp-", "steel" };
            Func<string, bool> looksRight = n =>
            {
                n = n.ToLower();
                return !bad.Any(b => n.Contains(b)) && (n.Contains("concrete") || n.Contains("rectangular") || n.Contains("rect"));
            };
            Func<BuiltInCategory, bool> has = cat => new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                .Cast<FamilySymbol>().Any(s => looksRight((s.FamilyName ?? "") + " " + (s.Name ?? "")));
            var rep = new List<string>();
            using (var t = new Transaction(doc, "CAD to Revit - load families"))
            {
                t.Start();
                var wants = new List<string[]> { new[] { "column", colPath }, new[] { "beam", beamPath } };
                if (!string.IsNullOrEmpty(footPath)) wants.Add(new[] { "footing", footPath });
                foreach (var w in wants)
                {
                    var cat = w[0] == "column" ? BuiltInCategory.OST_StructuralColumns : BuiltInCategory.OST_StructuralFraming;
                    if (w[0] == "footing" ? hasFooting() : has(cat)) continue;
                    if (string.IsNullOrEmpty(w[1]) || !File.Exists(w[1]))
                    {
                        rep.Add("REVIT No rectangular concrete " + w[0] + " family in the project, and Revit's library has none to load");
                        continue;
                    }
                    if (doc.LoadFamily(w[1], out Family f)) rep.Add("REVIT Loaded Revit's " + w[0] + " family '" + f.Name + "'");
                    else rep.Add("REVIT Could not load " + w[1]);
                }
                t.Commit();
            }
            return rep;
        }

        // As RUN_CS with the .NET load context: one revit\ script in IronPython 3.4, in doc.
        // Returns "OK\n<its log>" or "ERROR\n<the Python error>".
        static System.Runtime.Loader.AssemblyLoadContext alc;

        static string RunScript(Document doc, string ipDir, string libDir, string script, Dictionary<string, string> inputs)
        {
            Func<Type, string, Type[], System.Reflection.MethodInfo> find = (t, n, ps) => t.GetMethods().First(
                m => m.Name == n && !m.IsGenericMethodDefinition && m.GetParameters().Select(p => p.ParameterType).SequenceEqual(ps));
            object engine = null;
            Type et = null;
            try
            {
                if (alc == null)
                {
                    alc = new System.Runtime.Loader.AssemblyLoadContext("CAD to Revit - IronPython", false);
                    string dir = ipDir;
                    alc.Resolving += (ctx, an) =>
                    {
                        string p = Path.Combine(dir, an.Name + ".dll");
                        return File.Exists(p) ? ctx.LoadFromAssemblyPath(p) : null;
                    };
                    foreach (string n in new[] { "Microsoft.Scripting", "Microsoft.Dynamic", "IronPython", "IronPython.Modules" })
                        alc.LoadFromAssemblyPath(Path.Combine(ipDir, n + ".dll"));
                }
                var ipy = alc.Assemblies.First(a => a.GetName().Name == "IronPython");
                engine = ipy.GetType("IronPython.Hosting.Python").GetMethod("CreateEngine", Type.EmptyTypes).Invoke(null, null);
                et = engine.GetType();
                var paths = new List<string>((ICollection<string>)et.GetMethod("GetSearchPaths").Invoke(engine, null));
                if (!string.IsNullOrEmpty(libDir)) paths.Add(libDir);
                paths.Add(Path.GetDirectoryName(script));
                et.GetMethod("SetSearchPaths").Invoke(engine, new object[] { paths });
                object scope = et.GetMethod("CreateScope", Type.EmptyTypes).Invoke(engine, null);
                Type st = scope.GetType();
                var setVar = st.GetMethod("SetVariable", new[] { typeof(string), typeof(object) });
                setVar.Invoke(scope, new object[] { "__c2r_doc__", doc });
                setVar.Invoke(scope, new object[] { "__name__", "__main__" });
                setVar.Invoke(scope, new object[] { "__file__", script });
                foreach (var kv in inputs) setVar.Invoke(scope, new object[] { kv.Key, kv.Value });
                var exec = find(et, "Execute", new[] { typeof(string), st });
                exec.Invoke(engine, new object[] {
                    "class _C2RUI(object):\n    def __init__(self, d):\n        self.Document = d\n" +
                    "class _C2RApp(object):\n    def __init__(self, d):\n        self.ActiveUIDocument = _C2RUI(d)\n" +
                    "__revit__ = _C2RApp(__c2r_doc__)\n", scope });
                string code = "from __future__ import division; " + File.ReadAllText(script, System.Text.Encoding.UTF8);
                Type kindT = et.Assembly.GetType("Microsoft.Scripting.SourceCodeKind");
                object src = et.GetMethod("CreateScriptSourceFromString", new[] { typeof(string), typeof(string), kindT })
                    .Invoke(engine, new object[] { code, script, Enum.Parse(kindT, "File") });
                find(src.GetType(), "Execute", new[] { st }).Invoke(src, new object[] { scope });
                object outv = null;
                if ((bool)find(st, "ContainsVariable", new[] { typeof(string) }).Invoke(scope, new object[] { "OUT" }))
                    outv = find(st, "GetVariable", new[] { typeof(string) }).Invoke(scope, new object[] { "OUT" });
                return "OK\n" + Convert.ToString(outv);
            }
            catch (Exception ex)
            {
                Exception inner = ex;
                while (inner is System.Reflection.TargetInvocationException && inner.InnerException != null) inner = inner.InnerException;
                string msg = inner.ToString();
                try
                {
                    Type eoT = et.Assembly.GetType("Microsoft.Scripting.Hosting.ExceptionOperations");
                    object eo = et.GetMethods().First(m => m.Name == "GetService" && m.IsGenericMethodDefinition)
                        .MakeGenericMethod(eoT).Invoke(engine, new object[] { new object[0] });
                    string py = (string)find(eoT, "FormatException", new[] { typeof(Exception) }).Invoke(eo, new object[] { inner });
                    msg = py.Contains(inner.Message) ? py : py + "\n" + inner.GetType().Name + ": " + inner.Message;
                }
                catch (Exception) { }
                return "ERROR\n" + msg;
            }
        }
    }
}
