using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Diagnostics;
using System.Web.Script.Serialization;
using DirectShowLib;
using Capture3DS;
using Capture3DS.Cypress;
using Capture3DS.Ftd2;
using Capture3DS.Ftd3;
using Capture3DS.Loopy;

class CaptureCli
{
    internal static Capture3DSModel Model(string backend)
    {
        switch (backend)
        {
            case "ll-spa3": return Capture3DSModel.LlSpa3;
            case "ds-ftd2": return Capture3DSModel.DsFtd2;
            case "n3dsxl": return Capture3DSModel.N3dsxl;
            case "loopy-old3ds": return Capture3DSModel.LoopyOld3ds;
            default: throw new ArgumentException("Unknown capture backend: " + backend);
        }
    }

    internal static Capture3DSDeviceInfo[] List(string backend)
    {
        switch (Model(backend))
        {
            case Capture3DSModel.LlSpa3: return LlSpa3Device.ListDevices().ToArray();
            case Capture3DSModel.DsFtd2: return DsFtd2Device.ListDevices().ToArray();
            case Capture3DSModel.N3dsxl: return Ftd3N3dsxlDevice.ListDevices().ToArray();
            case Capture3DSModel.LoopyOld3ds: return LoopyOld3dsDevice.ListDevices().ToArray();
            default: throw new ArgumentException("Unsupported backend");
        }
    }

    internal static Capture3DSDeviceInfo Select(Capture3DSDeviceInfo[] devices, string backend, string stableId)
    {
        if (String.IsNullOrWhiteSpace(stableId))
            throw new ArgumentException("An exact StableId is required; run list BACKEND.");
        var matches = devices.Where(d => d.Model == Model(backend) &&
            !String.IsNullOrWhiteSpace(d.StableId) && d.StableId == stableId).ToArray();
        if (matches.Length != 1)
            throw new InvalidOperationException("Exactly one selected device is required; found " + matches.Length);
        return matches[0];
    }

    internal static string Json(object value) { return new JavaScriptSerializer().Serialize(value); }

    internal static void AtomicText(string path, string value)
    {
        string temp = path + ".next";
        File.WriteAllText(temp, value, new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temp, path, null); else File.Move(temp, path);
    }

    internal static Bitmap Mosaic(Capture3DSFrame frame)
    {
        int width, height;
        byte[] rgb = frame.ToMosaic(out width, out height);
        return FromRgb(rgb, width, height);
    }

    internal static Bitmap FromRgb(byte[] rgb, int width, int height)
    {
        var bitmap = new Bitmap(width, height, PixelFormat.Format24bppRgb);
        try
        {
            var data = bitmap.LockBits(new Rectangle(0, 0, width, height),
                ImageLockMode.WriteOnly, PixelFormat.Format24bppRgb);
            try
            {
                byte[] row = new byte[width * 3];
                for (int y = 0; y < height; y++)
                {
                    for (int x = 0; x < width; x++)
                    {
                        int a = (y * width + x) * 3, b = x * 3;
                        row[b] = rgb[a + 2]; row[b + 1] = rgb[a + 1]; row[b + 2] = rgb[a];
                    }
                    Marshal.Copy(row, 0, IntPtr.Add(data.Scan0, y * data.Stride), row.Length);
                }
            }
            finally { bitmap.UnlockBits(data); }
            return bitmap;
        }
        catch { bitmap.Dispose(); throw; }
    }

    internal sealed class StreamOptions
    {
        internal int SaveIntervalMs = 150;
        internal Size Size = Size.Empty;
        internal bool Mjpg;
        // null, or the screens fed to the OBS browser source: both, top or bottom.
        internal string ObsScreen;
    }

    // Options after "stream BACKEND OUTPUT_DIR STABLE_ID SESSION_TOKEN".
    internal static StreamOptions Options(string[] args)
    {
        var options = new StreamOptions();
        for (int i = 5; i < args.Length; i++)
        {
            if (args[i] == "--save-interval-ms" && i + 1 < args.Length)
            {
                int milliseconds;
                if (!Int32.TryParse(args[++i], out milliseconds) || milliseconds < 20 || milliseconds > 10000)
                    throw new ArgumentException("Use --save-interval-ms INTEGER in 20..10000 (default 150).");
                options.SaveIntervalMs = milliseconds;
            }
            else if (args[i] == "--size" && i + 1 < args.Length)
            {
                string[] parts = args[++i].Split('x');
                int width, height;
                if (parts.Length != 2 || !Int32.TryParse(parts[0], out width) || !Int32.TryParse(parts[1], out height)
                    || width < 1 || height < 1)
                    throw new ArgumentException("Use --size WIDTHxHEIGHT, such as 1920x1080.");
                options.Size = new Size(width, height);
            }
            else if (args[i] == "--mjpg")
            {
                options.Mjpg = true;
            }
            else if (args[i] == "--obs" && i + 1 < args.Length)
            {
                string screen = args[++i];
                if (screen != "both" && screen != "top" && screen != "bottom")
                    throw new ArgumentException("Use --obs both, --obs top or --obs bottom.");
                options.ObsScreen = screen;
            }
            else
            {
                throw new ArgumentException("Unknown stream option: " + args[i]);
            }
        }
        return options;
    }

    internal static int SaveInterval(string[] args)
    {
        return Options(args).SaveIntervalMs;
    }

    internal static void Publish(string output, Bitmap bitmap, object status)
    {
        string temp = Path.Combine(output, "frame.next.png"), target = Path.Combine(output, "latest.png");
        bitmap.Save(temp, ImageFormat.Png);
        if (File.Exists(target)) File.Replace(temp, target, null); else File.Move(temp, target);
        AtomicText(Path.Combine(output, "status.json"), Json(status));
    }

    internal static object FrameStatus(Capture3DSFrame frame, Bitmap bitmap, string session, string backend,
        string device, long frameNo, string capturedUtc, int saveIntervalMs)
    {
        return new {
            session = session, backend = backend, device = device,
            frame = frameNo, utc = DateTime.UtcNow.ToString("o"),
            captured_utc = capturedUtc, save_interval_ms = saveIntervalMs,
            width = bitmap.Width, height = bitmap.Height,
            top = new { x = (bitmap.Width - frame.TopWidth) / 2, y = 0,
                width = frame.TopWidth, height = frame.TopHeight },
            bottom = new { x = (bitmap.Width - frame.BottomWidth) / 2, y = frame.TopHeight,
                width = frame.BottomWidth, height = frame.BottomHeight }
        };
    }

    static void Connect(ICapture3DSDevice device, CancellationToken token)
    {
        var cancellable = device as ICancellableCapture3DSDevice;
        if (cancellable != null) cancellable.Connect(token); else device.Connect();
    }

    // One frame, or null. Two failed reads in a row reopen the device: a USB read fails now
    // and then (FT_OTHER_ERROR 0x20 on the N3DSXL), and ending the capture for it stopped OBS streams.
    // A device that cannot be released still ends the capture, so it is never opened twice.
    static Capture3DSFrame ReadFrame(ref ICapture3DSDevice device, ref int failures, Func<ICapture3DSDevice> open,
        string backend, CancellationToken token)
    {
        if (device == null)
        {
            ICapture3DSDevice opened = null;
            try
            {
                opened = open();
                Connect(opened, token);
            }
            catch (Exception)
            {
                if (opened != null) opened.Dispose();
                token.ThrowIfCancellationRequested();
                token.WaitHandle.WaitOne(200);
                return null;
            }
            device = opened;
            failures = 0;
            Console.WriteLine(DateTime.UtcNow.ToString("o") + " Reconnected " + backend + ".");
            return null;
        }
        try
        {
            var cancellable = device as ICancellableCapture3DSDevice;
            var frame = cancellable != null ? cancellable.ReadFrame(token) : device.ReadFrame();
            if (frame != null) failures = 0;
            return frame;
        }
        catch (Exception error)
        {
            token.ThrowIfCancellationRequested();
            if (++failures < 2)
            {
                token.WaitHandle.WaitOne(5);
                return null;
            }
            Console.WriteLine(DateTime.UtcNow.ToString("o") + " Reconnecting " + backend + " after "
                + error.GetType().Name + ": " + error.Message);
            ICapture3DSDevice broken = device;
            device = null;
            failures = 0;
            broken.Dispose();
            return null;
        }
    }

    internal static void Stream(Func<ICapture3DSDevice> open, string output, string backend, string session,
        int saveIntervalMs = 150, Func<long> elapsedMilliseconds = null, string obsScreen = null)
    {
        if (saveIntervalMs < 20 || saveIntervalMs > 10000)
            throw new ArgumentOutOfRangeException("saveIntervalMs", "Expected 20..10000 milliseconds.");
        var clock = Stopwatch.StartNew();
        if (elapsedMilliseconds == null) elapsedMilliseconds = () => clock.ElapsedMilliseconds;
        string stop = Path.Combine(output, "stop-" + session + ".request");
        ICapture3DSDevice device = open();
        using (var cancellation = new CancellationTokenSource())
        {
            ConsoleCancelEventHandler cancel = (s, e) => { e.Cancel = true; cancellation.Cancel(); };
            Console.CancelKeyPress += cancel;
            try
            {
                // Monitor stop requests even during a cancellable Connect/ReadFrame call.
                using (var timer = new Timer(_ => {
                    if (File.Exists(stop))
                        try { cancellation.Cancel(); } catch (ObjectDisposedException) { }
                }, null, 0, 100))
                {
                    if (File.Exists(stop)) return;
                    string stableId = device.Info.StableId;
                    var process = Process.GetCurrentProcess();
                    AtomicText(Path.Combine(output, "process.json"), Json(new {
                        pid = process.Id, start_filetime = process.StartTime.ToUniversalTime().ToFileTimeUtc(),
                        session = session, backend = backend, device = stableId,
                        save_interval_ms = saveIntervalMs, obs = obsScreen
                    }));
                    Connect(device, cancellation.Token);
                    Console.WriteLine("Connected " + backend + ". Stop with Ctrl+C or the session-specific stop file.");
                    int failures = 0;
                    if (obsScreen != null)
                    {
                        // Every frame carries its audio, so the reader only hands frames over;
                        // encoding and file writes happen on the feed's own thread.
                        using (var feed = new ObsFeed(output, session, backend, stableId,
                            saveIntervalMs, obsScreen, elapsedMilliseconds))
                        {
                            while (!cancellation.IsCancellationRequested && !File.Exists(stop))
                            {
                                var frame = ReadFrame(ref device, ref failures, open, backend, cancellation.Token);
                                if (frame != null) feed.Push(frame);
                            }
                        }
                        return;
                    }
                    long frameNo = 0;
                    int publicationErrors = 0;
                    long nextSave = 0;
                    while (!cancellation.IsCancellationRequested && !File.Exists(stop))
                    {
                        var frame = ReadFrame(ref device, ref failures, open, backend, cancellation.Token);
                        if (frame == null) continue;
                        frameNo++;
                        long now = elapsedMilliseconds();
                        if (now < nextSave) continue;
                        nextSave = now + saveIntervalMs;
                        string capturedUtc = DateTime.UtcNow.ToString("o");
                        using (var bitmap = Mosaic(frame))
                        {
                            try
                            {
                                Publish(output, bitmap, FrameStatus(frame, bitmap, session, backend,
                                    stableId, frameNo, capturedUtc, saveIntervalMs));
                                publicationErrors = 0;
                            }
                            catch (IOException)
                            {
                                // Brief snapshot-reader sharing conflicts must not reset USB capture.
                                if (++publicationErrors >= 20) throw;
                            }
                        }
                    }
                }
            }
            finally
            {
                Console.CancelKeyPress -= cancel;
                if (device != null) device.Dispose();
            }
        }
    }

    // HDMI/UVC capture boards for GC and Switch. One screen, published as is.
    internal static void StreamDshow(string stableId, string output, string session, StreamOptions options)
    {
        var clock = Stopwatch.StartNew();
        string stop = Path.Combine(output, "stop-" + session + ".request");
        var cancellation = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (s, e) => { e.Cancel = true; cancellation.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            if (File.Exists(stop)) return;
            using (var capture = DshowCapture.Open(stableId, options.Size, options.Mjpg))
            {
                var process = Process.GetCurrentProcess();
                AtomicText(Path.Combine(output, "process.json"), Json(new {
                    pid = process.Id, start_filetime = process.StartTime.ToUniversalTime().ToFileTimeUtc(),
                    session = session, backend = "dshow", device = stableId,
                    save_interval_ms = options.SaveIntervalMs, format = capture.FormatText,
                    requested_format_applied = capture.RequestedFormatApplied
                }));
                capture.Run();
                Console.WriteLine("Connected dshow (" + capture.FormatText + "). Stop with Ctrl+C or the session-specific stop file.");
                long lastFrame = 0, nextSave = 0;
                int publicationErrors = 0;
                while (!cancellation.IsCancellationRequested && !File.Exists(stop))
                {
                    if (clock.ElapsedMilliseconds < nextSave)
                    {
                        Thread.Sleep(2);
                        continue;
                    }
                    string capturedUtc;
                    long frameNo;
                    using (var bitmap = capture.TakeNewer(lastFrame, out frameNo, out capturedUtc))
                    {
                        if (bitmap == null)
                        {
                            Thread.Sleep(2);
                            continue;
                        }
                        lastFrame = frameNo;
                        nextSave = clock.ElapsedMilliseconds + options.SaveIntervalMs;
                        try
                        {
                            Publish(output, bitmap, new {
                                session = session, backend = "dshow", device = stableId,
                                frame = frameNo, utc = DateTime.UtcNow.ToString("o"),
                                captured_utc = capturedUtc, save_interval_ms = options.SaveIntervalMs,
                                width = bitmap.Width, height = bitmap.Height, format = capture.FormatText,
                                screen = new { x = 0, y = 0, width = bitmap.Width, height = bitmap.Height }
                            });
                            publicationErrors = 0;
                        }
                        catch (IOException)
                        {
                            if (++publicationErrors >= 20) throw;
                        }
                    }
                }
            }
        }
        finally
        {
            Console.CancelKeyPress -= cancel;
            cancellation.Dispose();
        }
    }

    internal static string MutexName(string stableId)
    {
        using (var hash = SHA256.Create())
            return @"Local\Standalone3DS_" +
                BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(stableId))).Replace("-", "");
    }

    internal static Mutex AcquireDevice(string stableId)
    {
        var mutex = new Mutex(false, MutexName(stableId));
        bool owned;
        try { owned = mutex.WaitOne(0); }
        catch (AbandonedMutexException) { owned = true; }
        if (!owned) { mutex.Dispose(); throw new IOException("This device is already owned by another standalone capture session."); }
        return mutex;
    }

    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 2 && args[0] == "list" && args[1] == "dshow")
            {
                var found = DshowCapture.List();
                foreach (var d in found)
                    Console.WriteLine(Json(new { backend = "dshow", name = d[0], stable_id = d[1] }));
                if (found.Count == 0) Console.WriteLine("[]");
                return 0;
            }
            if (args.Length == 2 && args[0] == "list")
            {
                var devices = List(args[1]);
                foreach (var d in devices)
                    Console.WriteLine(Json(new { backend = args[1], model = d.Model.ToString(),
                        serial = d.Serial, stable_id = d.StableId, description = d.Description,
                        super_speed = d.SuperSpeed }));
                if (devices.Length == 0) Console.WriteLine("[]");
                return 0;
            }
            if (args.Length == 2 && args[0] == "diagnose" && args[1] == "n3dsxl")
            {
                foreach (var line in Ftd3N3dsxlDevice.DescribeRawDevices()) Console.WriteLine(line);
                return 0;
            }
            if (args.Length < 5 || args[0] != "stream")
            {
                Console.Error.WriteLine("CaptureCli list BACKEND | stream BACKEND OUTPUT_DIR STABLE_ID SESSION_TOKEN [--save-interval-ms 20..10000] [--size WxH] [--mjpg] [--obs both|top|bottom]");
                Console.Error.WriteLine("BACKEND: ll-spa3 | ds-ftd2 | n3dsxl | loopy-old3ds | dshow (--size/--mjpg are dshow only, --obs is not for dshow)");
                Console.Error.WriteLine("CaptureCli diagnose n3dsxl : enumerate raw FTDI metadata without opening devices");
                return 2;
            }
            var options = Options(args);
            int saveIntervalMs = options.SaveIntervalMs;
            Guid parsed;
            if (!Guid.TryParseExact(args[4], "N", out parsed))
                throw new ArgumentException("SESSION_TOKEN must be a 32-character GUID.");
            string backend = args[1], output = Path.GetFullPath(args[2]);
            if (backend == "dshow")
            {
                if (options.ObsScreen != null)
                    throw new ArgumentException("--obs is only for the 3DS/DS backends; add an HDMI capture board to OBS directly.");
                if (String.IsNullOrWhiteSpace(args[3]))
                    throw new ArgumentException("An exact StableId is required; run list dshow.");
                Directory.CreateDirectory(output);
                using (var lease = new FileStream(Path.Combine(output, "session.lock"),
                    FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
                using (var mutex = AcquireDevice(args[3]))
                {
                    try { StreamDshow(args[3], output, args[4], options); }
                    finally { mutex.ReleaseMutex(); }
                }
                Console.WriteLine("Capture stopped and device released.");
                return 0;
            }
            if (!options.Size.IsEmpty || options.Mjpg)
                throw new ArgumentException("--size and --mjpg are only for the dshow backend.");
            Model(backend);
            Directory.CreateDirectory(output);
            // Exclusive file sharing protects the output directory across processes.
            using (var lease = new FileStream(Path.Combine(output, "session.lock"),
                FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
            {
                var selected = Select(List(backend), backend, args[3]);
                using (var mutex = AcquireDevice(selected.StableId))
                {
                    try
                    {
                        FtdOwnership.AssertAvailable(selected);
                        Stream(() => Capture3DSApi.Open(selected), output, backend, args[4], saveIntervalMs, null, options.ObsScreen);
                    }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            Console.WriteLine("Capture stopped and device released.");
            return 0;
        }
        catch (OperationCanceledException) { Console.WriteLine("Capture cancelled and device released."); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1; }
    }
}

// Feeds the OBS browser source page (obs.html) through one file, obs-live.json: the newest screen as
// JPEG and the last 0.3 s of PCM with the index of its first sample, so a missed read loses no audio.
// The file is overwritten in place, because replacing it makes OBS's local file reader fail and stall
// for about 0.8 s. "seq" is written first and "end" last, so the page can reject a read that overlapped
// a write. latest.png and status.json keep their usual interval and format, on their own thread:
// replacing latest.png sometimes takes 200-350 ms, and OBS must not wait for it.
internal sealed class ObsFeed : IDisposable
{
    // Every second frame of a 60 fps capture (frames are 16.7 ms apart, so 33 ms would often skip to the third).
    internal const int PacketIntervalMs = 30;
    readonly object gate = new object();
    readonly AutoResetEvent arrived = new AutoResetEvent(false);
    readonly AutoResetEvent saveArrived = new AutoResetEvent(false);
    readonly Thread worker, saver;
    readonly Func<long> elapsed;
    readonly string output, session, backend, device, screen;
    readonly int saveIntervalMs;
    readonly ImageCodecInfo jpeg = Array.Find(ImageCodecInfo.GetImageEncoders(), codec => codec.MimeType == "image/jpeg");
    readonly short[] audio;
    Capture3DSFrame latest;
    string latestUtc;
    long frames, packets, audioEnd;
    int audioLength;
    int rateNumerator = Capture3DSAudioChunk.DsSampleRateNumerator, rateDenominator = Capture3DSAudioChunk.DsSampleRateDenominator;
    bool stopping;
    Exception failure;

    internal ObsFeed(string output, string session, string backend, string device, int saveIntervalMs,
        string screen, Func<long> elapsed)
    {
        this.output = output;
        this.session = session;
        this.backend = backend;
        this.device = device;
        this.saveIntervalMs = saveIntervalMs;
        this.screen = screen;
        this.elapsed = elapsed;
        audio = new short[(int)(0.3 * rateNumerator / rateDenominator) * 2];
        worker = new Thread(Run) { IsBackground = true, Name = "OBS feed" };
        saver = new Thread(RunSaves) { IsBackground = true, Name = "Snapshot" };
        worker.Start();
        saver.Start();
    }

    internal void Push(Capture3DSFrame frame)
    {
        string utc = DateTime.UtcNow.ToString("o");
        lock (gate)
        {
            if (failure != null) throw new IOException("OBS feed failed: " + failure.Message, failure);
            latest = frame;
            latestUtc = utc;
            frames++;
            if (frame.Audio != null)
            {
                rateNumerator = frame.Audio.SampleRateNumerator;
                rateDenominator = frame.Audio.SampleRateDenominator;
                Append(frame.Audio.Samples);
            }
        }
        arrived.Set();
        saveArrived.Set();
    }

    // Keeps only the newest window of interleaved samples.
    void Append(short[] samples)
    {
        if (samples.Length >= audio.Length)
        {
            Buffer.BlockCopy(samples, (samples.Length - audio.Length) * 2, audio, 0, audio.Length * 2);
            audioLength = audio.Length;
        }
        else
        {
            int keep = Math.Min(audioLength, audio.Length - samples.Length);
            Buffer.BlockCopy(audio, (audioLength - keep) * 2, audio, 0, keep * 2);
            Buffer.BlockCopy(samples, 0, audio, keep * 2, samples.Length * 2);
            audioLength = keep + samples.Length;
        }
        audioEnd += samples.Length / 2;
    }

    void Run()
    {
        long packetFrame = 0, nextPacket = 0;
        try
        {
            while (true)
            {
                arrived.WaitOne(5);
                long now = elapsed();
                Capture3DSFrame frame;
                long number, pcmStart = 0;
                string utc;
                byte[] pcm = null;
                int numerator = 0, denominator = 0;
                bool packetDue;
                lock (gate)
                {
                    if (stopping) return;
                    frame = latest;
                    number = frames;
                    utc = latestUtc;
                    packetDue = number > packetFrame && now >= nextPacket;
                    if (packetDue)
                    {
                        pcm = new byte[audioLength * 2];
                        Buffer.BlockCopy(audio, 0, pcm, 0, pcm.Length);
                        pcmStart = audioEnd - audioLength / 2;
                        numerator = rateNumerator;
                        denominator = rateDenominator;
                    }
                }
                if (packetDue)
                {
                    nextPacket = now + PacketIntervalMs;
                    packetFrame = number;
                    WritePacket(frame, number, utc, pcm, pcmStart, numerator, denominator);
                }
            }
        }
        catch (Exception error)
        {
            lock (gate) failure = error;
        }
    }

    void RunSaves()
    {
        long savedFrame = 0, nextSave = 0;
        int saveErrors = 0;
        try
        {
            while (true)
            {
                saveArrived.WaitOne(5);
                long now = elapsed();
                Capture3DSFrame frame;
                long number;
                string utc;
                bool saveDue;
                lock (gate)
                {
                    if (stopping) return;
                    frame = latest;
                    number = frames;
                    utc = latestUtc;
                    saveDue = number > savedFrame && now >= nextSave;
                }
                if (saveDue)
                {
                    nextSave = now + saveIntervalMs;
                    savedFrame = number;
                    using (var bitmap = CaptureCli.Mosaic(frame))
                    {
                        try
                        {
                            CaptureCli.Publish(output, bitmap, CaptureCli.FrameStatus(frame, bitmap, session, backend,
                                device, number, utc, saveIntervalMs));
                            saveErrors = 0;
                        }
                        catch (IOException)
                        {
                            // Brief snapshot-reader sharing conflicts must not reset USB capture.
                            if (++saveErrors >= 20) throw;
                        }
                    }
                }
            }
        }
        catch (Exception error)
        {
            lock (gate) failure = error;
        }
    }

    void WritePacket(Capture3DSFrame frame, long number, string utc, byte[] pcm, long pcmStart,
        int numerator, int denominator)
    {
        Bitmap bitmap;
        if (screen == "top") bitmap = CaptureCli.FromRgb(frame.Top, frame.TopWidth, frame.TopHeight);
        else if (screen == "bottom") bitmap = CaptureCli.FromRgb(frame.Bottom, frame.BottomWidth, frame.BottomHeight);
        else bitmap = CaptureCli.Mosaic(frame);
        string image;
        using (bitmap)
        using (var memory = new MemoryStream())
        using (var quality = new EncoderParameters(1))
        {
            quality.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 95L);
            bitmap.Save(memory, jpeg, quality);
            image = "data:image/jpeg;base64," + Convert.ToBase64String(memory.GetBuffer(), 0, (int)memory.Length);
        }
        long seq = ++packets;
        string text = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue }.Serialize(new {
            seq = seq, session = session, frame = number, captured_utc = utc, screen = screen, image = image,
            audio = new { start = pcmStart, rate_numerator = numerator, rate_denominator = denominator,
                channels = 2, data = Convert.ToBase64String(pcm) },
            end = seq
        });
        byte[] bytes = new UTF8Encoding(false).GetBytes(text);
        try
        {
            using (var stream = new FileStream(Path.Combine(output, "obs-live.json"), FileMode.OpenOrCreate,
                FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.SetLength(bytes.Length);
            }
        }
        catch (IOException)
        {
            // OBS has the file open; the next packet carries the same audio window again.
        }
    }

    public void Dispose()
    {
        lock (gate) stopping = true;
        arrived.Set();
        saveArrived.Set();
        if (!worker.Join(5000)) throw new IOException("The OBS feed did not stop within five seconds.");
        if (!saver.Join(5000)) throw new IOException("The snapshot writer did not stop within five seconds.");
        arrived.Dispose();
        saveArrived.Dispose();
    }
}

// FTDI enumeration only: refuse an already-open device before any capture initialization.
static class FtdOwnership
{
    internal static string CString(byte[] data)
    {
        int end = Array.IndexOf(data, (byte)0);
        return Encoding.ASCII.GetString(data, 0, end < 0 ? data.Length : end);
    }
    [DllImport("FTD3XX.dll", CallingConvention = CallingConvention.StdCall, EntryPoint = "FT_CreateDeviceInfoList")]
    static extern int D3List(out uint count);
    [DllImport("FTD3XX.dll", CallingConvention = CallingConvention.StdCall, EntryPoint = "FT_GetDeviceInfoDetail")]
    static extern int D3Info(uint index, out uint flags, out uint type, out uint id,
        IntPtr location, byte[] serial, byte[] description, out IntPtr handle);
    [DllImport("ftd2xx.dll", CallingConvention = CallingConvention.StdCall, EntryPoint = "FT_CreateDeviceInfoList")]
    static extern int D2List(out uint count);
    [DllImport("ftd2xx.dll", CallingConvention = CallingConvention.StdCall, EntryPoint = "FT_GetDeviceInfoDetail")]
    static extern int D2Info(uint index, out uint flags, out uint type, out uint id,
        out uint location, byte[] serial, byte[] description, out IntPtr handle);

    internal static void AssertAvailable(Capture3DSDeviceInfo selected)
    {
        bool d3 = selected.Model == Capture3DSModel.N3dsxl;
        if (!d3 && selected.Model != Capture3DSModel.DsFtd2) return;
        uint count;
        int status = d3 ? D3List(out count) : D2List(out count);
        if (status != 0) throw new IOException("FTDI ownership enumeration failed: " + status);
        int matches = 0;
        var observed = new System.Collections.Generic.List<string>();
        for (uint index = 0; index < count; index++)
        {
            uint flags, type, id, location;
            IntPtr handle;
            byte[] serial = new byte[17], description = new byte[65];
            status = d3 ? D3Info(index, out flags, out type, out id, IntPtr.Zero, serial, description, out handle)
                : D2Info(index, out flags, out type, out id, out location, serial, description, out handle);
            if (status != 0) throw new IOException("FTDI ownership detail failed: " + status);
            string value = CString(serial);
            observed.Add("serial='" + value + "',flags=0x" + flags.ToString("X"));
            if (value != selected.Serial) continue;
            matches++;
            // AN_379 defines Flags bit 0 as the open-state indicator. The detail
            // call's handle output is not an ownership test for an unopened device.
            if ((flags & 1U) != 0)
                throw new IOException("The selected FTDI capture device is already open (flags=0x" + flags.ToString("X") + "). Its owner was not changed.");
        }
        if (matches != 1) throw new IOException("Cannot establish an unambiguous FTDI device identity before opening. Expected '" + selected.Serial + "'; enumerated " + String.Join("; ", observed));
    }
}

// Windowless DirectShow graph: device -> (Color Space Converter) -> Sample Grabber (RGB24) -> Null Renderer.
public sealed class DshowCapture : ISampleGrabberCB, IDisposable
{
    static readonly Guid NullRendererClsid = new Guid("C1F400A4-3F08-11d3-9F0B-006008039E37");
    static readonly Guid ColorSpaceConverterClsid = new Guid("1643E180-90F5-11CE-97D5-00AA0055595A");
    readonly object gate = new object();
    IGraphBuilder graph;
    ICaptureGraphBuilder2 builder;
    ISampleGrabber grabber;
    IMediaControl control;
    byte[] incoming, latest;
    long sequence;
    string latestUtc;
    int stride;
    bool bottomUp;
    public int Width, Height;
    public string FormatText = "";
    public bool RequestedFormatApplied = true;

    // Friendly name and StableId ("Dshow:" + moniker display name) of each video input.
    internal static List<string[]> List()
    {
        var result = new List<string[]>();
        DsDevice[] devices = DsDevice.GetDevicesOfCat(FilterCategory.VideoInputDevice);
        try
        {
            foreach (DsDevice device in devices)
            {
                string path = device.DevicePath;
                if (!String.IsNullOrEmpty(path)) result.Add(new[] { device.Name, "Dshow:" + path });
            }
        }
        finally
        {
            foreach (DsDevice device in devices) device.Dispose();
        }
        return result;
    }

    static IBaseFilter Bind(string stableId)
    {
        DsDevice[] devices = DsDevice.GetDevicesOfCat(FilterCategory.VideoInputDevice);
        try
        {
            DsDevice selected = null;
            int matches = 0;
            foreach (DsDevice device in devices)
            {
                if ("Dshow:" + device.DevicePath == stableId)
                {
                    selected = device;
                    matches++;
                }
            }
            if (matches != 1)
                throw new InvalidOperationException("Exactly one selected device is required; found " + matches);
            Guid guid = typeof(IBaseFilter).GUID;
            object filter;
            selected.Mon.BindToObject(null, null, ref guid, out filter);
            return (IBaseFilter)filter;
        }
        finally
        {
            foreach (DsDevice device in devices) device.Dispose();
        }
    }

    // Switch to the fastest MJPG or uncompressed format at the size (Empty keeps the current size).
    // Returns false and leaves the device default when no format matches.
    static bool TrySelectFormat(IAMStreamConfig streamConfig, Size size, bool mjpg)
    {
        if (size.IsEmpty)
        {
            AMMediaType current;
            if (streamConfig.GetFormat(out current) < 0) return false;
            try
            {
                if (current.formatType != FormatType.VideoInfo) return false;
                var header = (VideoInfoHeader)Marshal.PtrToStructure(current.formatPtr, typeof(VideoInfoHeader));
                size = new Size(header.BmiHeader.Width, Math.Abs(header.BmiHeader.Height));
            }
            finally { DsUtils.FreeAMMediaType(current); }
        }
        int count, capsSize;
        if (streamConfig.GetNumberOfCapabilities(out count, out capsSize) < 0) return false;
        IntPtr caps = Marshal.AllocCoTaskMem(capsSize);
        AMMediaType best = null;
        long bestInterval = long.MaxValue;
        try
        {
            for (int i = 0; i < count; i++)
            {
                AMMediaType type;
                if (streamConfig.GetStreamCaps(i, out type, caps) < 0) continue;
                bool chosen = false;
                try
                {
                    bool formatMatches;
                    if (mjpg)
                    {
                        formatMatches = type.subType == MediaSubType.MJPG;
                    }
                    else
                    {
                        formatMatches = type.subType == MediaSubType.YUY2 || type.subType == MediaSubType.NV12 || type.subType == MediaSubType.UYVY
                            || type.subType == MediaSubType.RGB24 || type.subType == MediaSubType.RGB32;
                    }
                    if (type.formatType == FormatType.VideoInfo && formatMatches)
                    {
                        var header = (VideoInfoHeader)Marshal.PtrToStructure(type.formatPtr, typeof(VideoInfoHeader));
                        var capability = (VideoStreamConfigCaps)Marshal.PtrToStructure(caps, typeof(VideoStreamConfigCaps));
                        long interval = capability.MinFrameInterval > 0 ? capability.MinFrameInterval : header.AvgTimePerFrame;
                        if (header.BmiHeader.Width == size.Width && Math.Abs(header.BmiHeader.Height) == size.Height
                            && interval > 0 && interval < bestInterval)
                        {
                            header.AvgTimePerFrame = interval;
                            Marshal.StructureToPtr(header, type.formatPtr, false);
                            if (best != null) DsUtils.FreeAMMediaType(best);
                            best = type;
                            bestInterval = interval;
                            chosen = true;
                        }
                    }
                }
                finally
                {
                    if (!chosen) DsUtils.FreeAMMediaType(type);
                }
            }
            return best != null && streamConfig.SetFormat(best) >= 0;
        }
        finally
        {
            Marshal.FreeCoTaskMem(caps);
            if (best != null) DsUtils.FreeAMMediaType(best);
        }
    }

    static string Describe(IAMStreamConfig streamConfig, int width, int height)
    {
        string text = width + "x" + height;
        AMMediaType actual;
        if (streamConfig == null || streamConfig.GetFormat(out actual) < 0) return text;
        try
        {
            if (actual.formatType != FormatType.VideoInfo) return text;
            var header = (VideoInfoHeader)Marshal.PtrToStructure(actual.formatPtr, typeof(VideoInfoHeader));
            string format;
            if (actual.subType == MediaSubType.RGB24) format = "RGB24";
            else if (actual.subType == MediaSubType.RGB32) format = "RGB32";
            else format = Encoding.ASCII.GetString(actual.subType.ToByteArray(), 0, 4);
            text = header.BmiHeader.Width + "x" + Math.Abs(header.BmiHeader.Height) + " " + format;
            if (header.AvgTimePerFrame > 0) text += " " + (10000000.0 / header.AvgTimePerFrame).ToString("0.#") + "fps";
            return text;
        }
        finally { DsUtils.FreeAMMediaType(actual); }
    }

    static IBaseFilter Create(Guid clsid)
    {
        return (IBaseFilter)Activator.CreateInstance(Type.GetTypeFromCLSID(clsid));
    }

    internal static DshowCapture Open(string stableId, Size size, bool mjpg)
    {
        var capture = new DshowCapture();
        IBaseFilter source = null, renderer = null, converter = null;
        try
        {
            capture.graph = (IGraphBuilder)new FilterGraph();
            capture.builder = (ICaptureGraphBuilder2)new CaptureGraphBuilder2();
            DsError.ThrowExceptionForHR(capture.builder.SetFiltergraph(capture.graph));
            source = Bind(stableId);
            DsError.ThrowExceptionForHR(capture.graph.AddFilter(source, "Video Capture"));
            object config;
            if (capture.builder.FindInterface(PinCategory.Preview, MediaType.Video, source, typeof(IAMStreamConfig).GUID, out config) < 0
                && capture.builder.FindInterface(PinCategory.Capture, MediaType.Video, source, typeof(IAMStreamConfig).GUID, out config) < 0)
            {
                config = null;
            }
            var streamConfig = config as IAMStreamConfig;
            if (!size.IsEmpty || mjpg)
            {
                capture.RequestedFormatApplied = streamConfig != null && TrySelectFormat(streamConfig, size, mjpg);
            }

            capture.grabber = (ISampleGrabber)new SampleGrabber();
            var requested = new AMMediaType();
            try
            {
                requested.majorType = MediaType.Video;
                requested.subType = MediaSubType.RGB24;
                requested.formatType = FormatType.VideoInfo;
                DsError.ThrowExceptionForHR(capture.grabber.SetMediaType(requested));
            }
            finally { DsUtils.FreeAMMediaType(requested); }
            renderer = Create(NullRendererClsid);
            DsError.ThrowExceptionForHR(capture.graph.AddFilter((IBaseFilter)capture.grabber, "Sample Grabber"));
            DsError.ThrowExceptionForHR(capture.graph.AddFilter(renderer, "Null Renderer"));
            if (capture.builder.RenderStream(PinCategory.Preview, MediaType.Video, source, (IBaseFilter)capture.grabber, renderer) < 0)
            {
                // Devices that cannot output RGB24 directly go through the Color Space Converter.
                DsError.ThrowExceptionForHR(capture.graph.RemoveFilter((IBaseFilter)capture.grabber));
                DsError.ThrowExceptionForHR(capture.graph.RemoveFilter(renderer));
                converter = Create(ColorSpaceConverterClsid);
                DsError.ThrowExceptionForHR(capture.graph.AddFilter(converter, "Color Space Converter"));
                DsError.ThrowExceptionForHR(capture.graph.AddFilter((IBaseFilter)capture.grabber, "Sample Grabber"));
                DsError.ThrowExceptionForHR(capture.graph.AddFilter(renderer, "Null Renderer"));
                DsError.ThrowExceptionForHR(capture.builder.RenderStream(PinCategory.Preview, MediaType.Video, source, converter, (IBaseFilter)capture.grabber));
                DsError.ThrowExceptionForHR(capture.builder.RenderStream(null, MediaType.Video, capture.grabber, null, renderer));
            }

            var connected = new AMMediaType();
            try
            {
                DsError.ThrowExceptionForHR(capture.grabber.GetConnectedMediaType(connected));
                var header = (VideoInfoHeader)Marshal.PtrToStructure(connected.formatPtr, typeof(VideoInfoHeader));
                capture.Width = header.BmiHeader.Width;
                capture.Height = Math.Abs(header.BmiHeader.Height);
                capture.bottomUp = header.BmiHeader.Height > 0;
                capture.stride = (capture.Width * 3 + 3) & ~3;
            }
            finally { DsUtils.FreeAMMediaType(connected); }
            capture.FormatText = Describe(streamConfig, capture.Width, capture.Height);
            capture.control = (IMediaControl)capture.graph;
            DsError.ThrowExceptionForHR(capture.grabber.SetBufferSamples(false));
            DsError.ThrowExceptionForHR(capture.grabber.SetOneShot(false));
            DsError.ThrowExceptionForHR(capture.grabber.SetCallback(capture, 1));
            return capture;
        }
        catch
        {
            capture.Dispose();
            throw;
        }
        finally
        {
            Release(converter);
            Release(renderer);
            Release(source);
        }
    }

    internal void Run()
    {
        DsError.ThrowExceptionForHR(control.Run());
    }

    int ISampleGrabberCB.SampleCB(double sampleTime, IMediaSample sample)
    {
        return 0;
    }

    int ISampleGrabberCB.BufferCB(double sampleTime, IntPtr buffer, int length)
    {
        int needed = stride * Height;
        if (length < needed) return 0;
        if (incoming == null || incoming.Length != needed) incoming = new byte[needed];
        Marshal.Copy(buffer, incoming, 0, needed);
        lock (gate)
        {
            byte[] swap = latest;
            latest = incoming;
            incoming = swap;
            sequence++;
            latestUtc = DateTime.UtcNow.ToString("o");
        }
        return 0;
    }

    // The newest frame as a top-down bitmap, or null when nothing arrived after the given frame number.
    internal Bitmap TakeNewer(long after, out long frame, out string capturedUtc)
    {
        lock (gate)
        {
            frame = sequence;
            capturedUtc = latestUtc;
            if (latest == null || sequence <= after) return null;
            var bitmap = new Bitmap(Width, Height, PixelFormat.Format24bppRgb);
            var data = bitmap.LockBits(new Rectangle(0, 0, Width, Height), ImageLockMode.WriteOnly, PixelFormat.Format24bppRgb);
            try
            {
                for (int y = 0; y < Height; y++)
                {
                    int row = bottomUp ? Height - 1 - y : y;
                    Marshal.Copy(latest, row * stride, IntPtr.Add(data.Scan0, y * data.Stride), Width * 3);
                }
            }
            finally { bitmap.UnlockBits(data); }
            return bitmap;
        }
    }

    public void Dispose()
    {
        if (grabber != null)
        {
            try { grabber.SetCallback(null, 0); } catch { }
        }
        if (control != null)
        {
            try { control.Stop(); } catch { }
        }
        Release(grabber);
        Release(builder);
        Release(graph);
        grabber = null;
        builder = null;
        graph = null;
        control = null;
    }

    static void Release(object value)
    {
        if (value == null || !Marshal.IsComObject(value)) return;
        try { Marshal.ReleaseComObject(value); } catch { }
    }
}

