using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Drawing;
using System.Drawing.Imaging;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Capture3DS;

class CaptureCliTests
{
    static int passed;
    static void Check(bool condition, string message)
    { if (!condition) throw new Exception(message); passed++; }

    static Capture3DSDeviceInfo Info(Capture3DSModel model, string serial)
    { return new Capture3DSDeviceInfo(model, serial, "test", false); }

    static Capture3DSFrame Frame(bool ds)
    {
        return Frame(ds, null);
    }

    static Capture3DSFrame Frame(bool ds, short[] samples)
    {
        int tw = ds ? 256 : 400, bw = ds ? 256 : 320, h = ds ? 192 : 240;
        byte[] top = new byte[tw * h * 3], bottom = new byte[bw * h * 3];
        for (int i = 0; i < top.Length; i += 3) top[i] = 255;
        for (int i = 0; i < bottom.Length; i += 3) bottom[i + 2] = 255;
        return new Capture3DSFrame(top, tw, h, bottom, bw, h, samples == null ? null : new Capture3DSAudioChunk(samples));
    }

    // The newest obs-live.json once it reports the given frame, read the way OBS does (sharing writes).
    static Dictionary<string, object> ObsPacket(string directory, long frame)
    {
        string path = Path.Combine(directory, "obs-live.json");
        var deadline = DateTime.UtcNow.AddSeconds(3);
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                string text;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(stream))
                    text = reader.ReadToEnd();
                var packet = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue }.DeserializeObject(text) as Dictionary<string, object>;
                if (packet != null && Convert.ToInt64(packet["frame"]) == frame) return packet;
            }
            catch (IOException) { }
            catch (ArgumentException) { }
            Thread.Sleep(10);
        }
        throw new Exception("No OBS packet for frame " + frame);
    }

    class Response
    {
        internal int Status;
        internal Dictionary<string, string> Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        internal byte[] Body = new byte[0];
    }

    static Response ReadResponse(Stream stream, bool headOnly)
    {
        var head = new List<byte>();
        while (head.Count < 4 || head[head.Count - 4] != 13 || head[head.Count - 3] != 10
            || head[head.Count - 2] != 13 || head[head.Count - 1] != 10)
        {
            int value = stream.ReadByte();
            if (value < 0) throw new Exception("The connection closed before the response head ended");
            head.Add((byte)value);
        }
        string[] lines = Encoding.ASCII.GetString(head.ToArray()).Split(new[] { "\r\n" }, StringSplitOptions.None);
        var response = new Response { Status = Int32.Parse(lines[0].Split(' ')[1]) };
        for (int i = 1; i < lines.Length; i++)
        {
            int colon = lines[i].IndexOf(':');
            if (colon > 0) response.Headers[lines[i].Substring(0, colon)] = lines[i].Substring(colon + 1).Trim();
        }
        string length;
        if (headOnly || !response.Headers.TryGetValue("Content-Length", out length)) return response;
        response.Body = new byte[Int32.Parse(length)];
        for (int read = 0; read < response.Body.Length; )
        {
            int count = stream.Read(response.Body, read, response.Body.Length - read);
            if (count <= 0) throw new Exception("The response body ended early");
            read += count;
        }
        return response;
    }

    static Response Http(int port, string request, bool headOnly = false)
    {
        using (var client = new TcpClient("127.0.0.1", port))
        {
            client.ReceiveTimeout = 5000;
            NetworkStream stream = client.GetStream();
            byte[] bytes = Encoding.ASCII.GetBytes(request);
            stream.Write(bytes, 0, bytes.Length);
            return ReadResponse(stream, headOnly);
        }
    }

    // The packet of "/live", or null for 204.
    static Dictionary<string, object> Live(int port, string query)
    {
        var response = Http(port, "GET /live?" + query + " HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n");
        if (response.Status == 204) return null;
        if (response.Status != 200) throw new Exception("Live view answered " + response.Status);
        return new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue }.DeserializeObject(Encoding.UTF8.GetString(response.Body))
            as Dictionary<string, object>;
    }

    static Bitmap Picture(Dictionary<string, object> packet)
    {
        string image = (string)packet["image"];
        if (!image.StartsWith("data:image/jpeg;base64,")) throw new Exception("The live view image is not a JPEG data URL");
        return new Bitmap(new MemoryStream(Convert.FromBase64String(image.Substring(image.IndexOf(',') + 1))));
    }

    static short[] Samples(Dictionary<string, object> packet)
    {
        byte[] bytes = Convert.FromBase64String((string)((Dictionary<string, object>)packet["audio"])["data"]);
        var samples = new short[bytes.Length / 2];
        Buffer.BlockCopy(bytes, 0, samples, 0, bytes.Length);
        return samples;
    }

    class Fake : ICancellableCapture3DSDevice
    {
        internal bool Disposed, Connected;
        internal bool BlockConnect, BlockRead;
        internal string Stop;
        internal int Reads, Connects, Disposals;
        internal int StopAfterReads = 2;
        // Read numbers that fail the way a broken USB transfer does.
        internal int[] FailReads = new int[0];
        internal readonly ManualResetEvent Entered = new ManualResetEvent(false);
        public Capture3DSDeviceInfo Info { get { return CaptureCliTests.Info(Capture3DSModel.N3dsxl, "fake"); } }
        public void Connect() { Connect(CancellationToken.None); }
        public void Connect(CancellationToken token)
        {
            Connected = true;
            Connects++;
            if (BlockConnect) Block(token);
        }
        void Block(CancellationToken token)
        {
            Entered.Set();
            if (!token.WaitHandle.WaitOne(3000)) throw new Exception("Cancellation was not delivered");
            token.ThrowIfCancellationRequested();
        }
        public Capture3DSFrame ReadFrame() { return ReadFrame(CancellationToken.None); }
        public Capture3DSFrame ReadFrame(CancellationToken token)
        {
            if (BlockRead) Block(token);
            if (++Reads == StopAfterReads) File.WriteAllText(Stop, "stop");
            if (Array.IndexOf(FailReads, Reads) >= 0) throw new Capture3DSException("FT_GetOverlappedResult failed: 0x20");
            return Frame(false);
        }
        public void Dispose() { Disposed = true; Disposals++; }
    }

    static void Main()
    {
        string root = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "tests", "capture-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        CaptureCli.ViewAddress = IPAddress.Loopback;
        string[] streamArgs = { "stream", "ll-spa3", "output", "device", "session" };
        Check(CaptureCli.SaveInterval(streamArgs) == 150, "default save interval remains 150ms");
        foreach (string interval in new[] { "20", "30", "10000" })
        {
            var args = new[] { "stream", "ll-spa3", "output", "device", "session", "--save-interval-ms", interval };
            Check(CaptureCli.SaveInterval(args) == Int32.Parse(interval), "explicit save interval");
        }
        foreach (string interval in new[] { "19", "10001", "0", "NaN", "20.5" })
        {
            bool rejected = false;
            try { CaptureCli.SaveInterval(new[] { "stream", "ll-spa3", "out", "id", "token", "--save-interval-ms", interval }); }
            catch (ArgumentException) { rejected = true; }
            Check(rejected, "invalid interval must fail before device access");
        }
        var dshow = CaptureCli.Options(new[] { "stream", "dshow", "out", "id", "token", "--size", "1920x1080", "--mjpg", "--save-interval-ms", "30" });
        Check(dshow.Size == new Size(1920, 1080) && dshow.Mjpg && dshow.SaveIntervalMs == 30, "dshow size, MJPG and interval options");
        Check(CaptureCli.Options(streamArgs).Size.IsEmpty && !CaptureCli.Options(streamArgs).Mjpg, "no format request by default");
        foreach (var bad in new[] { new[] { "--size", "1920" }, new[] { "--size", "0x720" }, new[] { "--size" }, new[] { "--fps", "60" } })
        {
            bool rejected = false;
            var args = new System.Collections.Generic.List<string> { "stream", "dshow", "out", "id", "token" };
            args.AddRange(bad);
            try { CaptureCli.Options(args.ToArray()); }
            catch (ArgumentException) { rejected = true; }
            Check(rejected, "invalid dshow option must fail before device access");
        }
        Check(CaptureCli.Options(streamArgs).ObsScreen == null, "no OBS feed by default");
        foreach (string screen in new[] { "both", "top", "bottom" })
            Check(CaptureCli.Options(new[] { "stream", "n3dsxl", "out", "id", "token", "--obs", screen }).ObsScreen == screen,
                "OBS screen option");
        foreach (var bad in new[] { new[] { "--obs" }, new[] { "--obs", "left" } })
        {
            bool rejected = false;
            var args = new List<string> { "stream", "n3dsxl", "out", "id", "token" };
            args.AddRange(bad);
            try { CaptureCli.Options(args.ToArray()); }
            catch (ArgumentException) { rejected = true; }
            Check(rejected, "invalid OBS option must fail before device access");
        }
        Check(FtdOwnership.CString(new byte[] { 110, 120, 108, 0, 63, 127 }) == "nxl", "FTDI serial stops at first NUL, ignoring uninitialized tail");
        foreach (bool ds in new[] { false, true })
            using (var bitmap = CaptureCli.Mosaic(Frame(ds)))
            {
                Check(bitmap.Width == (ds ? 256 : 400) && bitmap.Height == (ds ? 384 : 480), "mosaic dimensions");
                Check(bitmap.GetPixel(0, 0).ToArgb() == Color.Red.ToArgb(), "RGB top");
                Check(bitmap.GetPixel(ds ? 0 : 40, ds ? 192 : 240).ToArgb() == Color.Blue.ToArgb(), "RGB bottom");
                if (!ds) Check(bitmap.GetPixel(39, 240).ToArgb() == Color.Black.ToArgb(), "3DS lower margin");
            }
        var one = Info(Capture3DSModel.N3dsxl, "one");
        Check(CaptureCli.Select(new[] { one }, "n3dsxl", one.StableId) == one, "selected identity");
        foreach (var values in new[] { new[] { one, one }, new Capture3DSDeviceInfo[0],
                                       new[] { Info(Capture3DSModel.DsFtd2, "one") } })
        {
            bool rejected = false;
            try { CaptureCli.Select(values, "n3dsxl", one.StableId); }
            catch (InvalidOperationException) { rejected = true; }
            Check(rejected, "ambiguous, missing, or wrong backend must fail");
        }
        bool emptyRejected = false;
        try { CaptureCli.Select(new[] { one }, "n3dsxl", ""); }
        catch (ArgumentException) { emptyRejected = true; }
        Check(emptyRejected, "no implicit device selection");

        using (var mutex = CaptureCli.AcquireDevice("fake-exclusive"))
        {
            bool blocked = false;
            var thread = new Thread(() => {
                try { using (var unwanted = CaptureCli.AcquireDevice("fake-exclusive")) { unwanted.ReleaseMutex(); } }
                catch (IOException) { blocked = true; }
            });
            thread.Start(); thread.Join();
            Check(blocked, "duplicate device session must fail");
            mutex.ReleaseMutex();
        }
        using (var mutex = CaptureCli.AcquireDevice("fake-exclusive")) { mutex.ReleaseMutex(); }

        string session = Guid.NewGuid().ToString("N");
        var fake = new Fake { Stop = Path.Combine(root, "stop-" + session + ".request") };
        // Another session's stop file must not cancel this session.
        File.WriteAllText(Path.Combine(root, "stop-other.request"), "stop");
        CaptureCli.Stream(() => fake, root, "n3dsxl", session);
        Check(fake.Connected && fake.Reads == 2 && fake.Disposed, "stream start/stop/dispose");
        Check(File.Exists(Path.Combine(root, "latest.png")), "frame publication");
        Check(File.ReadAllText(Path.Combine(root, "status.json")).Contains(session), "session-bound status");
        using (var image = new Bitmap(Path.Combine(root, "latest.png")))
            Check(image.GetPixel(40, 240).ToArgb() == Color.Blue.ToArgb(), "PNG round trip");

        // A synthetic monotonic clock proves the publication gate without timing-sensitive sleeps.
        foreach (int interval in new[] { 20, 30, 150 })
        {
            session = Guid.NewGuid().ToString("N");
            var paced = new Fake { Stop = Path.Combine(root, "stop-" + session + ".request"), StopAfterReads = 4 };
            CaptureCli.Stream(() => paced, root, "n3dsxl", session, interval, () => (paced.Reads - 1) * 10L);
            var status = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(Path.Combine(root, "status.json")))
                as System.Collections.Generic.Dictionary<string, object>;
            Check(Convert.ToInt32(status["frame"]) == (interval == 20 ? 3 : interval == 30 ? 4 : 1),
                "configured interval controls real publication");
            Check(Convert.ToInt32(status["save_interval_ms"]) == interval && status.ContainsKey("captured_utc"),
                "status reports configured interval and capture timestamp");
            Check(paced.Disposed && paced.Reads == 4, "fast stream stops and disposes");
            paced.Entered.Dispose();
        }

        // OBS feed: the packet has the chosen screen and the newest 0.3 s of PCM with its sample index.
        string obsDirectory = Path.Combine(root, "obs");
        Directory.CreateDirectory(obsDirectory);
        var obsClock = System.Diagnostics.Stopwatch.StartNew();
        string obsSession = Guid.NewGuid().ToString("N");
        const int perFrame = 547, obsFrames = 30;
        using (var feed = new ObsFeed(obsDirectory, obsSession, "n3dsxl", "fake", 150, "top", () => obsClock.ElapsedMilliseconds))
        {
            short value = 0;
            for (int i = 0; i < obsFrames; i++)
            {
                var samples = new short[perFrame * 2];
                for (int n = 0; n < samples.Length; n += 2) { samples[n] = value; samples[n + 1] = (short)-value; value++; }
                feed.Push(Frame(false, samples));
            }
            var packet = ObsPacket(obsDirectory, obsFrames);
            Check(Convert.ToInt64(packet["seq"]) == Convert.ToInt64(packet["end"]) && (string)packet["session"] == obsSession,
                "OBS packet is whole and session-bound");
            Check((string)packet["screen"] == "top", "OBS packet reports the screen");
            string image = (string)packet["image"];
            Check(image.StartsWith("data:image/jpeg;base64,"), "OBS image is a JPEG data URL");
            using (var memory = new MemoryStream(Convert.FromBase64String(image.Substring(image.IndexOf(',') + 1))))
            using (var bitmap = new Bitmap(memory))
            {
                Color pixel = bitmap.GetPixel(200, 120);
                Check(bitmap.Width == 400 && bitmap.Height == 240, "top screen only");
                Check(pixel.R > 230 && pixel.G < 30 && pixel.B < 30, "top screen colors");
            }
            var sound = (Dictionary<string, object>)packet["audio"];
            short[] pcm = new short[Convert.FromBase64String((string)sound["data"]).Length / 2];
            Buffer.BlockCopy(Convert.FromBase64String((string)sound["data"]), 0, pcm, 0, pcm.Length * 2);
            long start = Convert.ToInt64(sound["start"]);
            int window = (int)(0.3 * Capture3DSAudioChunk.DsSampleRateNumerator / Capture3DSAudioChunk.DsSampleRateDenominator);
            Check(pcm.Length == window * 2 && start == obsFrames * perFrame - window, "audio window and its first index");
            bool continuous = true;
            for (int n = 0; n < window; n++)
                if (pcm[n * 2] != (short)(start + n) || pcm[n * 2 + 1] != (short)-(start + n)) continuous = false;
            Check(continuous, "audio samples in order, left and right kept apart");
            Check(Convert.ToInt32(sound["rate_numerator"]) == 67027964 && Convert.ToInt32(sound["rate_denominator"]) == 2048,
                "audio rate");
        }
        Check(File.ReadAllText(Path.Combine(obsDirectory, "status.json")).Contains(obsSession)
            && File.Exists(Path.Combine(obsDirectory, "latest.png")), "OBS feed keeps latest.png and status.json");

        session = Guid.NewGuid().ToString("N");
        var obsStream = new Fake { Stop = Path.Combine(obsDirectory, "stop-" + session + ".request"), StopAfterReads = 3 };
        CaptureCli.Stream(() => obsStream, obsDirectory, "n3dsxl", session, 150, null, "both");
        Check(obsStream.Disposed && obsStream.Reads == 3, "OBS stream stops and disposes");
        obsStream.Entered.Dispose();

        // Two failed reads in a row reopen the device and the capture goes on, with and without OBS.
        foreach (string screen in new[] { null, "top" })
        {
            session = Guid.NewGuid().ToString("N");
            var flaky = new Fake { Stop = Path.Combine(root, "stop-" + session + ".request"), StopAfterReads = 6, FailReads = new[] { 2, 3 } };
            int opens = 0;
            CaptureCli.Stream(() => { opens++; return flaky; }, root, "n3dsxl", session, 150, null, screen);
            Check(opens == 2 && flaky.Connects == 2 && flaky.Disposals == 2 && flaky.Reads == 6,
                "two failed reads reopen the device and capture continues");
            flaky.Entered.Dispose();
        }
        session = Guid.NewGuid().ToString("N");
        var once = new Fake { Stop = Path.Combine(root, "stop-" + session + ".request"), StopAfterReads = 4, FailReads = new[] { 2 } };
        int reopened = 0;
        CaptureCli.Stream(() => { reopened++; return once; }, root, "n3dsxl", session);
        Check(reopened == 1 && once.Connects == 1 && once.Disposals == 1 && once.Reads == 4,
            "a single failed read is retried on the same device");
        once.Entered.Dispose();

        foreach (bool duringConnect in new[] { true, false })
        {
            session = Guid.NewGuid().ToString("N");
            var blocking = new Fake { BlockConnect = duringConnect, BlockRead = !duringConnect };
            string stop = Path.Combine(root, "stop-" + session + ".request");
            var stopper = new Thread(() => { if (blocking.Entered.WaitOne(2000)) File.WriteAllText(stop, "stop"); });
            stopper.Start();
            bool cancelled = false;
            try { CaptureCli.Stream(() => blocking, root, "n3dsxl", session); }
            catch (OperationCanceledException) { cancelled = true; }
            stopper.Join();
            Check(cancelled && blocking.Disposed, "stop must cancel blocking connect/read and dispose device");
            blocking.Entered.Dispose();
        }
        // Live view: the port option, the addresses it answers, and its HTTP answers.
        Check(CaptureCli.Options(streamArgs).ViewPort == 0, "no live view by default");
        Check(CaptureCli.Options(new[] { "stream", "dshow", "out", "id", "token", "--view", "8000" }).ViewPort == 8000,
            "live view port option");
        foreach (var bad in new[] { new[] { "--view" }, new[] { "--view", "80" }, new[] { "--view", "65536" }, new[] { "--view", "x" } })
        {
            bool rejected = false;
            var args = new List<string> { "stream", "dshow", "out", "id", "token" };
            args.AddRange(bad);
            try { CaptureCli.Options(args.ToArray()); }
            catch (ArgumentException) { rejected = true; }
            Check(rejected, "invalid live view port must fail before device access");
        }
        foreach (string address in new[] { "192.168.11.12", "10.1.2.3", "172.16.0.1", "172.31.255.254", "169.254.3.4",
                                           "100.64.0.1", "100.127.255.254", "127.0.0.1" })
            Check(LiveView.IsPrivate(IPAddress.Parse(address)), "home network or Tailscale address: " + address);
        foreach (string address in new[] { "8.8.8.8", "1.1.1.1", "172.15.255.255", "172.32.0.1", "100.63.255.255",
                                           "100.128.0.1", "192.169.0.1", "203.0.113.5" })
            Check(!LiveView.IsPrivate(IPAddress.Parse(address)), "internet address refused: " + address);

        long available = 1;
        var sourceSize = new Size(400, 480);
        LiveView.FrameSource synthetic = (long after, out long frame, out string utc) =>
        {
            frame = Interlocked.Read(ref available);
            utc = DateTime.UtcNow.ToString("o");
            if (frame <= after) return null;
            var bitmap = new Bitmap(sourceSize.Width, sourceSize.Height, PixelFormat.Format24bppRgb);
            using (var graphics = Graphics.FromImage(bitmap)) graphics.Clear(Color.Lime);
            return bitmap;
        };
        int viewPort;
        using (var view = new LiveView(IPAddress.Loopback, 0, CaptureCli.WebRoot, synthetic, "view-session"))
        {
            view.WaitMs = 300;
            viewPort = view.Port;
            byte[] page = File.ReadAllBytes(Path.Combine(CaptureCli.WebRoot, "view.html"));
            var index = Http(view.Port, "GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n");
            Check(index.Status == 200 && index.Body.SequenceEqual(page)
                && index.Headers["Content-Type"].StartsWith("text/html"), "the view page");
            var headOnly = Http(view.Port, "HEAD / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n", true);
            Check(headOnly.Status == 200 && Int32.Parse(headOnly.Headers["Content-Length"]) == page.Length, "HEAD answers the length only");
            Check(Http(view.Port, "GET /obs-audio.js HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n").Body
                .SequenceEqual(File.ReadAllBytes(Path.Combine(CaptureCli.WebRoot, "obs-audio.js"))), "the audio script");
            Check(Http(view.Port, "GET /latest.png HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n").Status == 404,
                "no other file is served");
            Check(Http(view.Port, "GET /../CaptureCli.cs HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n").Status == 404,
                "no path outside the pages");
            Check(Http(view.Port, "POST / HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n").Status == 405, "only GET and HEAD");
            Check(Http(view.Port, "HELLO\r\n\r\n").Status == 400, "a broken request");
            using (var client = new TcpClient("127.0.0.1", view.Port))
            {
                client.ReceiveTimeout = 5000;
                NetworkStream stream = client.GetStream();
                byte[] twice = Encoding.ASCII.GetBytes("GET /obs-audio.js HTTP/1.1\r\nHost: test\r\n\r\nGET / HTTP/1.1\r\nHost: test\r\n\r\n");
                stream.Write(twice, 0, twice.Length);
                var first = ReadResponse(stream, false);
                var second = ReadResponse(stream, false);
                Check(first.Status == 200 && second.Status == 200 && second.Body.SequenceEqual(page)
                    && first.Headers["Connection"] == "keep-alive", "two requests on one connection");
            }

            var packet = Live(view.Port, "session=&after=-1&audio=-1");
            Check(packet != null && (string)packet["session"] == "view-session" && Convert.ToInt64(packet["frame"]) == 1
                && packet["audio"] == null, "the first screen, without audio before any arrived");
            using (var bitmap = Picture(packet))
            {
                Color pixel = bitmap.GetPixel(200, 240);
                Check(bitmap.Width == 400 && bitmap.Height == 480 && pixel.G > 230 && pixel.R < 30 && pixel.B < 30,
                    "the screen as a JPEG of its own size");
            }
            long seq = Convert.ToInt64(packet["seq"]);
            Check(Live(view.Port, "session=view-session&after=" + seq + "&audio=-1") == null, "no newer screen: 204");
            Check(Convert.ToInt64(Live(view.Port, "session=old&after=" + (seq + 100) + "&audio=-1")["seq"]) == seq,
                "another session gets the newest screen at once");

            const int chunk = 547;
            short value = 0;
            Action<int> pushAudio = chunks =>
            {
                for (int c = 0; c < chunks; c++)
                {
                    var samples = new short[chunk * 2];
                    for (int n = 0; n < samples.Length; n += 2) { samples[n] = value; samples[n + 1] = (short)-value; value++; }
                    view.PushAudio(new Capture3DSAudioChunk(samples));
                }
            };
            pushAudio(30);
            Interlocked.Increment(ref available);
            packet = Live(view.Port, "session=view-session&after=" + seq + "&audio=-1");
            var sound = (Dictionary<string, object>)packet["audio"];
            short[] pcm = Samples(packet);
            int window = (int)(0.3 * Capture3DSAudioChunk.DsSampleRateNumerator / Capture3DSAudioChunk.DsSampleRateDenominator);
            long start = Convert.ToInt64(sound["start"]);
            Check(Convert.ToInt64(packet["seq"]) > seq && pcm.Length == window * 2 && start == 30 * chunk - window,
                "a new page gets the newest 0.3 s of audio");
            bool continuous = true;
            for (int n = 0; n < pcm.Length / 2; n++)
                if (pcm[n * 2] != (short)(start + n) || pcm[n * 2 + 1] != (short)-(start + n)) continuous = false;
            Check(continuous && Convert.ToInt32(sound["rate_numerator"]) == Capture3DSAudioChunk.DsSampleRateNumerator,
                "audio samples in order, left and right kept apart");

            long audioNext = start + pcm.Length / 2;
            seq = Convert.ToInt64(packet["seq"]);
            pushAudio(5);
            Interlocked.Increment(ref available);
            packet = Live(view.Port, "session=view-session&after=" + seq + "&audio=" + audioNext);
            pcm = Samples(packet);
            start = Convert.ToInt64(((Dictionary<string, object>)packet["audio"])["start"]);
            Check(start == audioNext && pcm.Length == 5 * chunk * 2 && pcm[0] == (short)audioNext,
                "the next answer continues the audio from the page's last sample, each sample once");

            seq = Convert.ToInt64(packet["seq"]);
            sourceSize = new Size(1920, 1080);
            Interlocked.Increment(ref available);
            packet = Live(view.Port, "session=view-session&after=" + seq + "&audio=-1");
            using (var bitmap = Picture(packet))
                Check(bitmap.Width == 1280 && bitmap.Height == 720, "a 1080p screen is sent at 1280x720");
        }
        var released = new TcpListener(IPAddress.Loopback, viewPort);
        released.Start();
        released.Stop();
        Check(true, "the live view releases its port");

        // The 3DS/DS stream starts the live view and reports it in process.json.
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        int streamPort = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        session = Guid.NewGuid().ToString("N");
        var viewed = new Fake { Stop = Path.Combine(root, "stop-" + session + ".request"), StopAfterReads = 3 };
        CaptureCli.Stream(() => viewed, root, "n3dsxl", session, 150, null, null, streamPort);
        var processInfo = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(Path.Combine(root, "process.json")))
            as Dictionary<string, object>;
        var viewInfo = processInfo["view"] as Dictionary<string, object>;
        Check(viewInfo != null && Convert.ToInt32(viewInfo["port"]) == streamPort && viewInfo["urls"] is object[],
            "process.json reports the live view");
        Check(viewed.Disposed && viewed.Reads == 3, "a stream with the live view stops and disposes");
        viewed.Entered.Dispose();
        released = new TcpListener(IPAddress.Loopback, streamPort);
        released.Start();
        released.Stop();
        Check(true, "the stream releases the live view port");

        fake.Entered.Dispose();
        // Test temp directory is retained as an inspectable PNG/status fixture.
        Console.WriteLine("PASS: " + passed + " capture assertions; no native devices opened.");
        Console.WriteLine("Fixture directory: " + root);
    }
}

