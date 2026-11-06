
import { useEffect, useState } from "react";
import { supabase } from "./lib/supabase";
import "./App.css";

function App() {
  const DOWNLOAD_URL =
    "https://github.com/RealChadOfNITA/NITA-JARVIS/releases/download/v1.0.0/NITA_JARVIS_main.exe";

  const [page, setPage] = useState("home");
  const [isRegister, setIsRegister] = useState(false);
  const [session, setSession] = useState(null);
  const [profile, setProfile] = useState(null);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [hostel, setHostel] = useState("");
  const [blockNumber, setBlockNumber] = useState("");
  const [roomNumber, setRoomNumber] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [profileLoading, setProfileLoading] = useState(false);
  const [telegramLoading, setTelegramLoading] = useState(false);
  const [telegramLink, setTelegramLink] = useState("");
  const [telegramConnected, setTelegramConnected] = useState(false);
  const [telegramStatusLoading, setTelegramStatusLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    supabase.auth.getSession().then(({ data, error }) => {
      if (!mounted) return;
      if (error) setMessage(error.message);
      setSession(data?.session ?? null);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession);
      if (newSession) setPage("dashboard");
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!session?.user) {
      setProfile(null);
      setTelegramConnected(false);
      setTelegramStatusLoading(false);
      return;
    }

    let active = true;

    async function loadProfile() {
      setProfileLoading(true);

      const { data, error } = await supabase
        .from("profiles")
        .select("full_name, hostel, block_number, room_number")
        .eq("id", session.user.id)
        .maybeSingle();

      if (!active) return;

      if (error) {
        setMessage("Could not load profile: " + error.message);
      } else {
        setProfile(data);
      }

      setProfileLoading(false);
    }

    async function checkTelegramStatus() {
      setTelegramStatusLoading(true);

      const { data, error } = await supabase.functions.invoke(
        "telegram-status"
      );

      if (!active) return;

      if (error) {
        console.error("Telegram status check failed:", error);
      } else {
        setTelegramConnected(data?.connected === true);
      }

      setTelegramStatusLoading(false);
    }

    loadProfile();
    checkTelegramStatus();

    return () => {
      active = false;
    };
  }, [session]);

  function openAuth(register = false) {
    setIsRegister(register);
    setMessage("");
    setPage("auth");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setMessage("");
    setLoading(true);

    try {
      if (isRegister) {
        const { data, error } = await supabase.auth.signUp({
          email,
          password,
          options: {
            data: {
              full_name: fullName,
              hostel,
              block_number: blockNumber,
              room_number: roomNumber,
            },
          },
        });

        if (error) throw error;

        if (data.session) {
          setMessage("Account created successfully!");
          setPage("dashboard");
        } else {
          setMessage(
            "Registration successful! Please confirm your email, then sign in."
          );
          setIsRegister(false);
        }
      } else {
        const { error } = await supabase.auth.signInWithPassword({
          email,
          password,
        });

        if (error) throw error;
        setMessage("");
      }
    } catch (error) {
      setMessage(error.message || "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  async function handleTelegramConnect() {
    setTelegramLoading(true);
    setTelegramLink("");
    setMessage("");

    try {
      const { data, error } = await supabase.functions.invoke(
        "create-telegram-link"
      );

      if (error) throw error;

      if (!data?.bot_url) {
        throw new Error("The server did not return a Telegram link.");
      }

      setTelegramLink(data.bot_url);
      setMessage("Your personal Telegram connection link is ready!");
    } catch (error) {
      console.error("Telegram connection error:", error);
      setMessage(
        error.message || "Could not create your Telegram connection link."
      );
    } finally {
      setTelegramLoading(false);
    }
  }

  async function handleLogout() {
    const { error } = await supabase.auth.signOut();

    if (error) {
      setMessage(error.message);
    } else {
      setMessage("");
      setProfile(null);
      setTelegramLink("");
      setPage("home");
    }
  }

  const displayName =
    profile?.full_name ||
    session?.user?.user_metadata?.full_name ||
    "Student";

  if (session) {
    return (
      <div className="site-shell">
        <header className="topbar">
          <button
            className="brand brand-button"
            onClick={() => setPage("dashboard")}
          >
            <span className="brand-mark">N</span>
            <span>
              NITA <b>JARVIS</b>
              <small>SMART HOSTEL SECURITY</small>
            </span>
          </button>

          <div className="topbar-actions">
            <span className="signed-in-label">STUDENT PORTAL</span>
            <button
              className="button button-outline button-small"
              onClick={handleLogout}
            >
              Log out
            </button>
          </div>
        </header>

        <main className="dashboard-page">
          <section className="dashboard-heading">
            <div>
              <p className="eyebrow">
                <span className="live-dot" /> SECURE STUDENT PORTAL
              </p>
              <h1>
                Welcome back, <span>{displayName}</span>
              </h1>
              <p className="muted">
                Your room security overview, all in one place.
              </p>
            </div>

            <div className="dashboard-chip">⌁ ACCOUNT ACTIVE</div>
          </section>

          {profileLoading ? (
            <div className="panel">Loading your student profile...</div>
          ) : (
            <section className="panel profile-panel">
              <div className="panel-title-row">
                <div>
                  <p className="eyebrow">YOUR INFORMATION</p>
                  <h2>Hostel profile</h2>
                </div>
                <span className="panel-symbol">⌂</span>
              </div>

              <div className="profile-details">
                <div>
                  <small>FULL NAME</small>
                  <strong>{profile?.full_name || displayName}</strong>
                </div>
                <div>
                  <small>HOSTEL</small>
                  <strong>{profile?.hostel || "Not available"}</strong>
                </div>
                <div>
                  <small>BLOCK</small>
                  <strong>{profile?.block_number || "Not available"}</strong>
                </div>
                <div>
                  <small>ROOM</small>
                  <strong>{profile?.room_number || "Not available"}</strong>
                </div>
                <div className="profile-email">
                  <small>EMAIL</small>
                  <strong>{session.user.email}</strong>
                </div>
              </div>
            </section>
          )}

          <div className="section-heading">
            <div>
              <p className="eyebrow">SYSTEM OVERVIEW</p>
              <h2>Your security services</h2>
            </div>
          </div>

          <section className="service-grid">
            <article className="service-card">
              <div className="service-icon blue-icon">◉</div>
              <span className="service-state state-pending">
                WINDOWS APPLICATION
              </span>
              <h3>Camera system</h3>
              <p>
                Download NITA JARVIS for Windows and connect your laptop
                camera to your student account.
              </p>
              <a
                className="button button-primary"
                href={DOWNLOAD_URL}
              >
                Download for Windows ↓
              </a>
              <div className="service-foot">
                Windows app <span>→</span>
              </div>
            </article>

            <article className="service-card">
              <div className="service-icon purple-icon">➤</div>

              <span
                className={`service-state ${
                  telegramConnected ? "state-online" : "state-pending"
                }`}
              >
                {telegramStatusLoading
                  ? "CHECKING"
                  : telegramConnected
                    ? "CONNECTED"
                    : "NOT CONNECTED"}
              </span>

              <h3>Telegram alerts</h3>
              <p>
                Receive security notifications directly through Telegram.
              </p>

              <button
                className="text-action"
                onClick={handleTelegramConnect}
                disabled={telegramLoading || telegramConnected}
              >
                {telegramLoading
                  ? "Creating link..."
                  : telegramConnected
                    ? "Telegram connected ✓"
                    : "Connect Telegram →"}
              </button>

              {telegramLink && (
                <div className="telegram-link">
                  <a
                    href={telegramLink}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open your personal Telegram link
                  </a>
                  <small>
                    Link expires in 10 minutes and can only be used once.
                  </small>
                </div>
              )}
            </article>

            <article className="service-card">
              <div className="service-icon green-icon">⌖</div>
              <span className="service-state state-pending">
                WAITING FOR CAMERA
              </span>
              <h3>Motion detection</h3>
              <p>
                Motion alerts become available when your Windows app is
                connected and armed.
              </p>
              <div className="service-foot">
                Monitoring inactive <span>·</span>
              </div>
            </article>
          </section>

          {message && (
            <p className="notice" role="status">
              {message}
            </p>
          )}

          <p className="dashboard-disclaimer">
            Camera monitoring is not active from this website. Use the NITA
            JARVIS Windows application to arm your room.
          </p>
        </main>

        <footer className="site-footer">
          <span>© 2026 NITA JARVIS</span>
          <span>SMART HOSTEL SECURITY</span>
        </footer>
      </div>
    );
  }

  if (page === "auth") {
    return (
      <div className="site-shell auth-shell">
        <header className="topbar">
          <button
            className="brand brand-button"
            onClick={() => setPage("home")}
          >
            <span className="brand-mark">N</span>
            <span>
              NITA <b>JARVIS</b>
              <small>SMART HOSTEL SECURITY</small>
            </span>
          </button>

          <button
            className="button button-ghost button-small"
            onClick={() => setPage("home")}
          >
            ← Back to home
          </button>
        </header>

        <main className="auth-layout">
          <section className="auth-intro">
            <p className="eyebrow">
              <span className="live-dot" /> STUDENT ACCESS
            </p>
            <h1>
              {isRegister ? (
                <>
                  Your room.
                  <br />
                  <span>Your security.</span>
                </>
              ) : (
                <>
                  Welcome
                  <br />
                  <span>back.</span>
                </>
              )}
            </h1>
            <p>
              Sign in to manage your NITA JARVIS account, connect Telegram
              alerts and set up your Windows security app.
            </p>
            <div className="auth-trust">◇ Secure account access</div>
          </section>

          <section className="auth-card">
            <div className="auth-card-heading">
              <p className="eyebrow">
                {isRegister ? "NEW STUDENT" : "STUDENT PORTAL"}
              </p>
              <h2>{isRegister ? "Create your account" : "Sign in"}</h2>
              <p>
                {isRegister
                  ? "Enter your details to get started."
                  : "Enter your account details to continue."}
              </p>
            </div>

            <form onSubmit={handleSubmit}>
              {isRegister && (
                <input
                  type="text"
                  placeholder="Full name"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  required
                />
              )}

              <input
                type="email"
                placeholder="College email address"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />

              <input
                type="password"
                placeholder="Password (at least 6 characters)"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={6}
                required
              />

              {isRegister && (
                <>
                  <label className="field-label" htmlFor="hostel">
                    Hostel
                  </label>

                  <select
                    id="hostel"
                    value={hostel}
                    onChange={(e) => setHostel(e.target.value)}
                    required
                  >
                    <option value="" disabled>
                      Select hostel
                    </option>
                    <option value="RNT hostel">RNT hostel</option>
                    <option value="Aryabhatta hostel">
                      Aryabhatta hostel
                    </option>
                    <option value="Gargi hostel">Gargi hostel</option>
                  </select>

                  <div className="field-row">
                    <input
                      type="text"
                      placeholder="Block number"
                      value={blockNumber}
                      onChange={(e) => setBlockNumber(e.target.value)}
                      required
                    />

                    <input
                      type="text"
                      placeholder="Room number"
                      value={roomNumber}
                      onChange={(e) => setRoomNumber(e.target.value)}
                      required
                    />
                  </div>
                </>
              )}

              <button
                className="button button-primary submit-button"
                type="submit"
                disabled={loading}
              >
                {loading
                  ? "PLEASE WAIT..."
                  : isRegister
                    ? "CREATE ACCOUNT →"
                    : "SIGN IN →"}
              </button>
            </form>

            {message && (
              <p className="notice" role="status">
                {message}
              </p>
            )}

            <p className="auth-switch">
              {isRegister ? "Already registered?" : "New student?"}
              <button
                onClick={() => {
                  setIsRegister(!isRegister);
                  setMessage("");
                }}
              >
                {isRegister ? "Sign in" : "Create an account"}
              </button>
            </p>

            <p className="privacy-note">
              Your account is protected by Supabase authentication.
            </p>
          </section>
        </main>

        <footer className="site-footer">
          <span>© 2026 NITA JARVIS</span>
          <span>SMART HOSTEL SECURITY</span>
        </footer>
      </div>
    );
  }

  return (
    <div className="site-shell">
      <header className="topbar">
        <a className="brand" href="#home">
          <span className="brand-mark">N</span>
          <span>
            NITA <b>JARVIS</b>
            <small>SMART HOSTEL SECURITY</small>
          </span>
        </a>

        <nav className="main-nav">
          <a href="#features">Features</a>
          <a href="#how-it-works">How it works</a>
        </nav>

        <div className="topbar-actions">
          <button
            className="button button-ghost button-small"
            onClick={() => openAuth(false)}
          >
            Student login
          </button>
          <button
            className="button button-primary button-small"
            onClick={() => openAuth(true)}
          >
            Get started ↗
          </button>
        </div>
      </header>

      <main id="home">
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">
              <span className="live-dot" /> SMART HOSTEL SECURITY PLATFORM
            </p>

            <h1>
              Your room.
              <br />
              Your <span>security.</span>
            </h1>

            <p className="hero-description">
              A smarter way to keep an eye on your hostel room. Turn your
              Windows laptop into a motion-aware security system and receive
              alerts through Telegram.
            </p>

            <div className="hero-actions">
              <button
                className="button button-primary button-large"
                onClick={() => openAuth(true)}
              >
                Get started →
              </button>

              <button
                className="button button-outline button-large"
                onClick={() => openAuth(false)}
              >
                Student login
              </button>

              <a
                className="button button-outline button-large"
                href={DOWNLOAD_URL}
              >
                Download for Windows ↓
              </a>
            </div>

            <div className="hero-proof">
              <span>✦</span> Designed for student room security
              <i />
              <span>⌁</span> Windows + Telegram
            </div>
          </div>

          <div className="hero-visual">
            <div className="orbit orbit-one"></div>
            <div className="orbit orbit-two"></div>

            <div className="scanner-card">
              <div className="scanner-top">
                <span>
                  <i className="live-dot" /> SYSTEM INTERFACE
                </span>
                <span className="scanner-status">STANDBY</span>
              </div>

              <div className="scanner-center">
                <div className="scanner-ring">
                  <div className="scanner-core">
                    N<span>J</span>
                  </div>
                  <div className="scan-line"></div>
                </div>
              </div>

              <div className="scanner-bottom">
                <span>
                  <small>SECURITY MODE</small>
                  <b>READY TO CONFIGURE</b>
                </span>
                <span className="signal-bars">
                  <i />
                  <i />
                  <i />
                  <i />
                </span>
              </div>
            </div>

            <div className="float-tag tag-top">⌁ MOTION AWARE</div>
            <div className="float-tag tag-bottom">➤ TELEGRAM ALERTS</div>
            <div className="visual-glow"></div>
          </div>
        </section>

        <section className="stats-strip">
          <div><strong>01</strong><span>Student account</span></div>
          <div><strong>02</strong><span>Windows app</span></div>
          <div><strong>03</strong><span>Telegram alerts</span></div>
          <div><strong>24/7</strong><span>When your device is running</span></div>
        </section>

        <section className="content-section" id="features">
          <div className="section-heading centered">
            <p className="eyebrow">BUILT FOR HOSTEL LIFE</p>
            <h2>
              Security, made <span>simple.</span>
            </h2>
            <p>
              One connected experience to help you monitor your room while
              you're away.
            </p>
          </div>

          <div className="feature-grid">
            <article className="feature-card">
              <div className="feature-icon blue-icon">◉</div>
              <p className="feature-number">01 / CAMERA</p>
              <h3>Webcam monitoring</h3>
              <p>
                Use your Windows laptop's webcam as the camera for your room
                security setup.
              </p>
            </article>

            <article className="feature-card">
              <div className="feature-icon purple-icon">⌖</div>
              <p className="feature-number">02 / DETECTION</p>
              <h3>Motion awareness</h3>
              <p>
                Get notified when the Windows app detects movement while your
                room is armed.
              </p>
            </article>

            <article className="feature-card">
              <div className="feature-icon green-icon">➤</div>
              <p className="feature-number">03 / ALERTS</p>
              <h3>Telegram notifications</h3>
              <p>
                Connect your Telegram account to receive alerts from your own
                security setup.
              </p>
            </article>
          </div>
        </section>

        <section className="how-section" id="how-it-works">
          <div className="how-copy">
            <p className="eyebrow">GET STARTED IN THREE STEPS</p>
            <h2>
              From setup to
              <br />
              <span>peace of mind.</span>
            </h2>
            <p>
              Create your student account, connect your Telegram and set up
              the Windows application.
            </p>
            <button
              className="button button-primary"
              onClick={() => openAuth(true)}
            >
              Create student account →
            </button>
          </div>

          <div className="steps">
            <div className="step">
              <span>01</span>
              <div>
                <h3>Create your account</h3>
                <p>Register with your email and hostel room details.</p>
              </div>
            </div>

            <div className="step">
              <span>02</span>
              <div>
                <h3>Connect Telegram</h3>
                <p>Link your Telegram account for security notifications.</p>
              </div>
            </div>

            <div className="step">
              <span>03</span>
              <div>
                <h3>Set up the Windows app</h3>
                <p>
                  Install the app, check your camera and arm your room when
                  you leave.
                </p>
              </div>
            </div>
          </div>
        </section>

        <section className="closing-cta">
          <p className="eyebrow">NITA JARVIS / STUDENT ACCESS</p>
          <h2>
            Make your room
            <br />
            <span>smarter to protect.</span>
          </h2>
          <button
            className="button button-primary button-large"
            onClick={() => openAuth(true)}
          >
            Get started →
          </button>
        </section>
      </main>

      <footer className="site-footer">
        <span>© 2026 NITA JARVIS</span>
        <span>SMART HOSTEL SECURITY</span>
        <button onClick={() => openAuth(false)}>Student login</button>
      </footer>
    </div>
  );
}

export default App;
