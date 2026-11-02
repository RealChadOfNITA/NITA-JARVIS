
import { useEffect, useState } from "react";
import { supabase } from "./lib/supabase";
import "./App.css";

function App() {
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
  if (!session?.user) {
    setTelegramConnected(false);
    setTelegramStatusLoading(false);
    return;
  }

  let active = true;

  async function checkTelegramStatus() {
    setTelegramStatusLoading(true);

    const { data, error } = await supabase.functions.invoke(
      "telegram-status"
    );

    if (!active) return;

    if (error) {
      console.error("Telegram status check failed:", error);
      setMessage("Could not check Telegram connection status.");
    } else {
      setTelegramConnected(data?.connected === true);
    }

    setTelegramStatusLoading(false);
  }

  checkTelegramStatus();

  return () => {
    active = false;
  };
}, [session]);


  useEffect(() => {
    supabase.auth.getSession().then(({ data, error }) => {
      if (error) {
        setMessage(error.message);
        return;
      }
      setSession(data.session);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession);
    });

    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (!session?.user) {
      setProfile(null);
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

    loadProfile();

    return () => {
      active = false;
    };
  }, [session]);

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
    }
  }

  if (session) {
    return (
      <div className="app">
        <div className="dashboard">
          <header className="dashboard-header">
            <div>
              <h1>NITA <span>JARVIS</span></h1>
              <p className="subtitle">SMART HOSTEL SECURITY</p>
            </div>
            <button className="logout-button" onClick={handleLogout}>
              LOG OUT
            </button>
          </header>

          <section className="dashboard-welcome">
            <p className="dashboard-label">STUDENT DASHBOARD</p>
            <h2>
              Welcome,{" "}
              {profile?.full_name ||
                session.user.user_metadata?.full_name ||
                "Student"}
            </h2>
            <p>Your hostel security overview.</p>
          </section>

          {profileLoading ? (
            <p>Loading your student profile...</p>
          ) : (
            <section className="profile-card">
              <h3>My Hostel Details</h3>
              <p><strong>Full name:</strong> {profile?.full_name || "Not available"}</p>
              <p><strong>Hostel:</strong> {profile?.hostel || "Not available"}</p>
              <p><strong>Block:</strong> {profile?.block_number || "Not available"}</p>
              <p><strong>Room:</strong> {profile?.room_number || "Not available"}</p>
              <p><strong>Email:</strong> {session.user.email}</p>
            </section>
          )}

          <section className="dashboard-grid">
            <div className="status-card">
              <div className="status-icon">📷</div>
              <h3>Camera</h3>
              <p>Not connected</p>
              <span>Windows app setup pending</span>
            </div>

            <div className="status-card">
              <div className="status-icon">✈️</div>
              <h3>Telegram</h3>
              <p>
  {telegramStatusLoading
    ? "Checking connection..."
    : telegramConnected
      ? "Connected ✓"
      : "Not connected"}
</p>

              <button
                className="login-button"
                onClick={handleTelegramConnect}
              disabled={telegramLoading || telegramConnected}
              >
                {telegramLoading ? "GENERATING LINK..." : "CONNECT TELEGRAM"}
              </button>

              {telegramLink && (
                <div style={{ marginTop: "15px" }}>
                  <p>Your personal connection link:</p>
                  <a
                    href={telegramLink}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    OPEN TELEGRAM
                  </a>
                  <p style={{ fontSize: "12px", marginTop: "8px" }}>
                    This link expires in 10 minutes and can only be used once.
                  </p>
                </div>
              )}
            </div>

            <div className="status-card">
              <div className="status-icon">🛡️</div>
              <h3>Motion Detection</h3>
              <p>Inactive</p>
              <span>Waiting for camera connection</span>
            </div>
          </section>

          {message && (
            <p role="status" style={{ marginTop: "15px" }}>
              {message}
            </p>
          )}

          <p className="dashboard-note">
            Your security dashboard is being set up. Camera monitoring is not
            active yet.
          </p>
        </div>

        <footer>NITA JARVIS © 2026 | SMART HOSTEL SECURITY</footer>
      </div>
    );
  }

  return (
    <div className="app">
      <div className="login-card">
        <div className="logo">🛡️</div>

        <h1>NITA <span>JARVIS</span></h1>
        <p className="subtitle">SMART HOSTEL SECURITY</p>

        <div className="welcome">
          <h2>{isRegister ? "Create your account" : "Welcome back"}</h2>
          <p>
            {isRegister
              ? "Register to secure your hostel room."
              : "Sign in to your security dashboard."}
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
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={6}
            required
          />

          {isRegister && (
            <>
              <select
                value={hostel}
                onChange={(e) => setHostel(e.target.value)}
                required
              >
                <option value="" disabled>Select hostel</option>
                <option value="RNT hostel">RNT hostel</option>
                <option value="Aryabhatta hostel">Aryabhatta hostel</option>
                <option value="Gargi hostel">Gargi hostel</option>
              </select>

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
            </>
          )}

          <button className="login-button" type="submit" disabled={loading}>
            {loading
              ? "PLEASE WAIT..."
              : isRegister
                ? "CREATE ACCOUNT"
                : "SIGN IN"}
          </button>
        </form>

        {message && (
          <p role="status" style={{ marginTop: "12px", fontSize: "13px" }}>
            {message}
          </p>
        )}

        <p className="switch">
          {isRegister ? "Already registered?" : "New student?"}
          <button
            type="button"
            onClick={() => {
              setIsRegister(!isRegister);
              setMessage("");
            }}
          >
            {isRegister ? "Sign in" : "Create account"}
          </button>
        </p>

        <div className="security">
          <span>●</span> Secure student access
        </div>
      </div>

      <footer>NITA JARVIS © 2026 | SMART HOSTEL SECURITY</footer>
    </div>
  );
}

export default App;
