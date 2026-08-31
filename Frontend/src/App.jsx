import { useEffect, useState } from "react";
import "./App.css";

const API_URL = "http://127.0.0.1:8000";

function App() {
  const [transactions, setTransactions] = useState([]);
  const [loading, setLoading] = useState(false);

  const [form, setForm] = useState({
    amount: "",
    description: "",
    category: "",
    payment_method: "UPI",
  });

  // Fetch transactions from backend
  const fetchTransactions = async () => {
    try {
      const response = await fetch(`${API_URL}/transactions`);

      if (!response.ok) {
        throw new Error("Failed to fetch transactions");
      }

      const data = await response.json();
      setTransactions(data);
    } catch (error) {
      console.error("Error:", error);
    }
  };

  useEffect(() => {
    fetchTransactions();
  }, []);

  // Handle form input
  const handleChange = (event) => {
    setForm({
      ...form,
      [event.target.name]: event.target.value,
    });
  };

  // Add manual transaction
  const addTransaction = async (event) => {
    event.preventDefault();

    if (!form.amount || !form.description) {
      alert("Please enter amount and description.");
      return;
    }

    try {
      const response = await fetch(`${API_URL}/transactions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          amount: Number(form.amount),
          description: form.description,
          category: form.category || "Other",
          payment_method: form.payment_method,
        }),
      });

      if (!response.ok) {
        throw new Error("Failed to add transaction");
      }

      setForm({
        amount: "",
        description: "",
        category: "",
        payment_method: "UPI",
      });

      fetchTransactions();
    } catch (error) {
      console.error("Error:", error);
      alert("Could not add transaction.");
    }
  };

  // Upload bank / UPI statement
  const uploadStatement = async (event) => {
    const file = event.target.files[0];

    if (!file) return;

    const formData = new FormData();
    formData.append("file", file);

    setLoading(true);

    try {
      const response = await fetch(`${API_URL}/upload`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        throw new Error("Upload failed");
      }

      const data = await response.json();

      console.log("Upload result:", data);

      alert("Statement uploaded successfully!");

      fetchTransactions();
    } catch (error) {
      console.error("Error:", error);
      alert("Could not upload statement.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app">
      {/* HEADER */}
      <header className="header">
        <div>
          <h1>💸 MoneyTrail</h1>
          <p>Track where your money goes.</p>
        </div>

        <label className="upload-button">
          {loading ? "Uploading..." : "📄 Upload Statement"}
          <input
            type="file"
            accept=".pdf,.csv,.xlsx,.xls"
            onChange={uploadStatement}
            hidden
          />
        </label>
      </header>

      <main className="container">
        {/* SUMMARY */}
        <section className="summary">
          <div className="summary-card">
            <span>Total Transactions</span>
            <strong>{transactions.length}</strong>
          </div>

          <div className="summary-card">
            <span>Total Spent</span>
            <strong>
              ₹
              {transactions
                .reduce((total, transaction) => {
                  return total + Number(transaction.amount || 0);
                }, 0)
                .toFixed(2)}
            </strong>
          </div>
        </section>

        {/* ADD TRANSACTION */}
        <section className="card">
          <h2>➕ Add Expense</h2>

          <form onSubmit={addTransaction} className="transaction-form">
            <input
              type="number"
              name="amount"
              placeholder="Amount (₹)"
              value={form.amount}
              onChange={handleChange}
            />

            <input
              type="text"
              name="description"
              placeholder="Description"
              value={form.description}
              onChange={handleChange}
            />

            <input
              type="text"
              name="category"
              placeholder="Category"
              value={form.category}
              onChange={handleChange}
            />

            <select
              name="payment_method"
              value={form.payment_method}
              onChange={handleChange}
            >
              <option value="UPI">UPI</option>
              <option value="Cash">Cash</option>
              <option value="Card">Card</option>
              <option value="Bank Transfer">Bank Transfer</option>
            </select>

            <button type="submit">Add Transaction</button>
          </form>
        </section>

        {/* TRANSACTIONS */}
        <section className="card">
          <div className="section-heading">
            <h2>📊 Your Transactions</h2>

            <button onClick={fetchTransactions} className="refresh-button">
              Refresh
            </button>
          </div>

          {transactions.length === 0 ? (
            <p className="empty">
              No transactions yet. Add one or upload your statement.
            </p>
          ) : (
            <div className="transaction-list">
              {transactions.map((transaction) => (
                <div
                  className="transaction"
                  key={transaction.id}
                >
                  <div>
                    <h3>{transaction.description}</h3>
                    <p>
                      {transaction.category || "Other"} ·{" "}
                      {transaction.payment_method || "Unknown"}
                    </p>
                  </div>

                  <strong>
                    ₹{Number(transaction.amount).toFixed(2)}
                  </strong>
                </div>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

export default App;