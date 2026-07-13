#ifndef MARKET_ENGINE_H
#define MARKET_ENGINE_H

#include <vector>
#include <string>
#include <cmath>
#include <random>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <numeric>
#include <deque>
#include <map>

// Lightweight online-learning neural network for market prediction
// Features: z-score, trend, volatility, digit distribution, momentum
// Output: trade signal (BUY_UP, BUY_DOWN, NO_TRADE) + confidence

struct TickData {
    double price;
    double epoch;
    int last_digit;
    double delta;
    double velocity;
};

struct FeatureVector {
    double z_score;
    double trend_slope;
    double volatility;
    double bb_position;
    double digit_bias;
    double momentum;
    double mean_reversion;
    double tick_acceleration;
    double price_range_pct;
    int regime; // 0=range, 1=trend, 2=digit_anomaly
};

struct TradeSignal {
    int direction;  // 1=UP, -1=DOWN, 0=NO_TRADE
    double confidence;
    double expected_value;
    std::string reason;
};

class MarketEngine {
private:
    // Online MLP: input(9) -> hidden(12) -> output(3)
    static const int INPUT_SIZE = 9;
    static const int HIDDEN_SIZE = 12;
    static const int OUTPUT_SIZE = 3; // UP, DOWN, NO_TRADE

    double W1[HIDDEN_SIZE][INPUT_SIZE];  // input -> hidden weights
    double b1[HIDDEN_SIZE];               // hidden bias
    double W2[OUTPUT_SIZE][HIDDEN_SIZE]; // hidden -> output weights
    double b2[OUTPUT_SIZE];               // output bias

    double learning_rate;
    int total_trades;
    int correct_predictions;
    double total_pnl;

    // Feature buffer
    std::deque<double> price_buffer;
    std::deque<TickData> tick_buffer;
    int buffer_size;

    // Online learning memory
    struct MemoryEntry {
        FeatureVector features;
        int actual_outcome; // 1=win, -1=loss, 0=unknown
        double pnl;
    };
    std::deque<MemoryEntry> memory;
    int max_memory;

    // Digit frequency
    int digit_counts[10];
    int digit_total;

    // Helper functions
    double sigmoid(double x) {
        return 1.0 / (1.0 + std::exp(-std::clamp(x, -10.0, 10.0)));
    }

    double relu(double x) {
        return x > 0 ? x : 0;
    }

    double tanh_activation(double x) {
        return std::tanh(std::clamp(x, -10.0, 10.0));
    }

    double random_weight() {
        static std::mt19937 gen(42);
        static std::normal_distribution<> dist(0.0, 0.3);
        return dist(gen);
    }

    // Forward pass
    std::vector<double> forward(const std::vector<double>& input) {
        // Hidden layer
        double hidden[HIDDEN_SIZE];
        for (int i = 0; i < HIDDEN_SIZE; i++) {
            hidden[i] = b1[i];
            for (int j = 0; j < INPUT_SIZE; j++) {
                hidden[i] += W1[i][j] * input[j];
            }
            hidden[i] = relu(hidden[i]);
        }

        // Output layer
        std::vector<double> output(OUTPUT_SIZE);
        for (int i = 0; i < OUTPUT_SIZE; i++) {
            output[i] = b2[i];
            for (int j = 0; j < HIDDEN_SIZE; j++) {
                output[i] += W2[i][j] * hidden[j];
            }
            output[i] = sigmoid(output[i]);
        }
        return output;
    }

    // Online backpropagation
    void train_step(const std::vector<double>& input, int target_class, double reward) {
        // Forward
        double hidden[HIDDEN_SIZE];
        for (int i = 0; i < HIDDEN_SIZE; i++) {
            hidden[i] = b1[i];
            for (int j = 0; j < INPUT_SIZE; j++) {
                hidden[i] += W1[i][j] * input[j];
            }
            hidden[i] = relu(hidden[i]);
        }

        double output_raw[OUTPUT_SIZE];
        double output[OUTPUT_SIZE];
        for (int i = 0; i < OUTPUT_SIZE; i++) {
            output_raw[i] = b2[i];
            for (int j = 0; j < HIDDEN_SIZE; j++) {
                output_raw[i] += W2[i][j] * hidden[j];
            }
            output[i] = sigmoid(output_raw[i]);
        }

        // Output error (with reward scaling)
        double delta_out[OUTPUT_SIZE];
        for (int i = 0; i < OUTPUT_SIZE; i++) {
            double target = (i == target_class) ? 1.0 : 0.0;
            delta_out[i] = (target - output[i]) * output[i] * (1.0 - output[i]) * reward;
        }

        // Hidden error
        double delta_hidden[HIDDEN_SIZE];
        for (int j = 0; j < HIDDEN_SIZE; j++) {
            double err = 0;
            for (int i = 0; i < OUTPUT_SIZE; i++) {
                err += delta_out[i] * W2[i][j];
            }
            delta_hidden[j] = (hidden[j] > 0) ? err : 0; // relu derivative
        }

        // Update weights W2
        for (int i = 0; i < OUTPUT_SIZE; i++) {
            for (int j = 0; j < HIDDEN_SIZE; j++) {
                W2[i][j] += learning_rate * delta_out[i] * hidden[j];
            }
            b2[i] += learning_rate * delta_out[i];
        }

        // Update weights W1
        for (int j = 0; j < HIDDEN_SIZE; j++) {
            for (int k = 0; k < INPUT_SIZE; k++) {
                W1[j][k] += learning_rate * delta_hidden[j] * input[k];
            }
            b1[j] += learning_rate * delta_hidden[j];
        }
    }

public:
    MarketEngine() : learning_rate(0.01), total_trades(0), correct_predictions(0),
                     total_pnl(0), buffer_size(200), max_memory(1000), digit_total(0) {
        // Initialize weights
        for (int i = 0; i < HIDDEN_SIZE; i++) {
            for (int j = 0; j < INPUT_SIZE; j++) W1[i][j] = random_weight();
            b1[i] = 0.0;
        }
        for (int i = 0; i < OUTPUT_SIZE; i++) {
            for (int j = 0; j < HIDDEN_SIZE; j++) W2[i][j] = random_weight();
            b2[i] = 0.0;
        }
        for (int i = 0; i < 10; i++) digit_counts[i] = 0;
    }

    // Add tick data
    void add_tick(double price, double epoch) {
        TickData tick;
        tick.price = price;
        tick.epoch = epoch;
        tick.last_digit = static_cast<int>(std::fmod(std::abs(price * 1000), 10));
        tick.delta = 0;
        tick.velocity = 0;

        if (!tick_buffer.empty()) {
            tick.delta = price - tick_buffer.back().price;
            tick.velocity = (epoch - tick_buffer.back().epoch) * 1000;
        }

        tick_buffer.push_back(tick);
        price_buffer.push_back(price);

        // Update digit frequency
        digit_counts[tick.last_digit]++;
        digit_total++;

        // Trim buffers
        while (static_cast<int>(tick_buffer.size()) > buffer_size) tick_buffer.pop_front();
        while (static_cast<int>(price_buffer.size()) > buffer_size) price_buffer.pop_front();
    }

    // Extract features from current buffer
    FeatureVector extract_features() {
        FeatureVector fv;
        fv.z_score = 0;
        fv.trend_slope = 0;
        fv.volatility = 0;
        fv.bb_position = 0.5;
        fv.digit_bias = 0;
        fv.momentum = 0;
        fv.mean_reversion = 0;
        fv.tick_acceleration = 0;
        fv.price_range_pct = 0;
        fv.regime = 0;

        if (static_cast<int>(price_buffer.size()) < 20) return fv;

        // Compute stats
        double sum = 0, sum_sq = 0;
        int n = price_buffer.size();
        for (double p : price_buffer) { sum += p; sum_sq += p * p; }
        double mean = sum / n;
        double var = sum_sq / n - mean * mean;
        double std = std::sqrt(std::max(var, 1e-10));

        // Z-score
        fv.z_score = (price_buffer.back() - mean) / std;

        // Trend (linear regression slope)
        double x_mean = (n - 1) / 2.0;
        double y_mean = mean;
        double num = 0, den = 0;
        for (int i = 0; i < n; i++) {
            num += (i - x_mean) * (price_buffer[i] - y_mean);
            den += (i - x_mean) * (i - x_mean);
        }
        fv.trend_slope = den > 0 ? num / den : 0;

        // Volatility (recent vs overall)
        int recent_n = std::min(50, n);
        double recent_sum = 0, recent_sum_sq = 0;
        for (int i = n - recent_n; i < n; i++) {
            recent_sum += price_buffer[i];
            recent_sum_sq += price_buffer[i] * price_buffer[i];
        }
        double recent_mean = recent_sum / recent_n;
        double recent_var = recent_sum_sq / recent_n - recent_mean * recent_mean;
        double recent_std = std::sqrt(std::max(recent_var, 1e-10));
        fv.volatility = recent_std / std;

        // Bollinger Band position
        double bb_upper = recent_mean + 2 * recent_std;
        double bb_lower = recent_mean - 2 * recent_std;
        double bb_width = bb_upper - bb_lower;
        fv.bb_position = bb_width > 0 ? (price_buffer.back() - bb_lower) / bb_width : 0.5;

        // Digit bias (max deviation from uniform)
        double max_dev = 0;
        for (int i = 0; i < 10; i++) {
            double freq = digit_total > 0 ? (double)digit_counts[i] / digit_total : 0.1;
            double dev = std::abs(freq - 0.1);
            if (dev > max_dev) max_dev = dev;
        }
        fv.digit_bias = max_dev;

        // Momentum (recent direction persistence)
        int wins = 0;
        for (int i = std::max(0, n - 20); i < n - 1; i++) {
            if (price_buffer[i + 1] > price_buffer[i]) wins++;
        }
        fv.momentum = (double)wins / 20.0 - 0.5; // -0.5 to +0.5

        // Mean reversion signal
        fv.mean_reversion = -fv.z_score * fv.volatility;

        // Tick acceleration
        if (tick_buffer.size() >= 3) {
            double v1 = tick_buffer[tick_buffer.size() - 1].velocity;
            double v2 = tick_buffer[tick_buffer.size() - 2].velocity;
            fv.tick_acceleration = v2 > 0 ? (v1 - v2) / v2 : 0;
        }

        // Price range %
        double high = *std::max_element(price_buffer.begin(), price_buffer.end());
        double low = *std::min_element(price_buffer.begin(), price_buffer.end());
        fv.price_range_pct = mean > 0 ? (high - low) / mean : 0;

        // Regime classification
        if (fv.volatility < 0.8 && std::abs(fv.z_score) < 0.8 && std::abs(fv.trend_slope) < 0.0005)
            fv.regime = 0; // RANGE
        else if (std::abs(fv.trend_slope) > 0.001 && std::abs(fv.z_score) > 0.5)
            fv.regime = 1; // TREND
        else if (fv.digit_bias > 0.03)
            fv.regime = 2; // DIGIT_ANOMALY
        else
            fv.regime = 0;

        return fv;
    }

    // Convert features to neural network input
    std::vector<double> features_to_input(const FeatureVector& fv) {
        std::vector<double> input(INPUT_SIZE);
        input[0] = std::tanh(fv.z_score);           // [-1, 1]
        input[1] = std::tanh(fv.trend_slope * 5000); // scaled
        input[2] = std::tanh(fv.volatility - 1);     // centered at 0
        input[3] = fv.bb_position * 2 - 1;           // [-1, 1]
        input[4] = fv.digit_bias * 10;               // scaled
        input[5] = fv.momentum * 2;                  // [-1, 1]
        input[6] = std::tanh(fv.mean_reversion);     // [-1, 1]
        input[7] = std::tanh(fv.tick_acceleration);  // [-1, 1]
        input[8] = fv.price_range_pct * 20;          // scaled
        return input;
    }

    // Get trade signal
    TradeSignal predict() {
        FeatureVector fv = extract_features();
        std::vector<double> input = features_to_input(fv);
        std::vector<double> output = forward(input);

        TradeSignal signal;
        signal.direction = 0;
        signal.confidence = 0;
        signal.expected_value = 0;

        double max_prob = std::max({output[0], output[1], output[2]});

        if (output[0] > output[1] && output[0] > 0.55) {
            // UP signal
            signal.direction = 1;
            signal.confidence = output[0];
            signal.expected_value = output[0] * 0.95 - (1 - output[0]);
            signal.reason = "z=" + std::to_string(fv.z_score) + " trend=" + std::to_string(fv.trend_slope);
        } else if (output[1] > output[0] && output[1] > 0.55) {
            // DOWN signal
            signal.direction = -1;
            signal.confidence = output[1];
            signal.expected_value = output[1] * 0.95 - (1 - output[1]);
            signal.reason = "z=" + std::to_string(fv.z_score) + " trend=" + std::to_string(fv.trend_slope);
        } else {
            // NO TRADE
            signal.direction = 0;
            signal.confidence = output[2];
            signal.expected_value = 0;
            signal.reason = "low_confidence";
        }

        return signal;
    }

    // Learn from trade result
    void learn(double profit, double stake) {
        FeatureVector fv = extract_features();
        std::vector<double> input = features_to_input(fv);

        total_trades++;
        total_pnl += profit;
        if (profit > 0) correct_predictions++;

        // Determine target class from recent price movement
        int target = 2; // default: no trade
        if (price_buffer.size() >= 2) {
            double last_move = price_buffer.back() - price_buffer[price_buffer.size() - 2];
            if (last_move > 0) target = 0; // UP
            else if (last_move < 0) target = 1; // DOWN
        }

        // Reward based on profit
        double reward = profit > 0 ? 1.5 : 0.5;

        // Online training
        train_step(input, target, reward);

        // Store in memory for replay
        MemoryEntry entry;
        entry.features = fv;
        entry.actual_outcome = profit > 0 ? 1 : -1;
        entry.pnl = profit;
        memory.push_back(entry);
        if (static_cast<int>(memory.size()) > max_memory) memory.pop_front();

        // Periodic replay training
        if (total_trades % 10 == 0 && memory.size() > 20) {
            replay_train(10);
        }
    }

    // Replay training from memory
    void replay_train(int batch_size) {
        std::mt19937 gen(total_trades);
        std::uniform_int_distribution<> dist(0, memory.size() - 1);

        for (int i = 0; i < batch_size && i < (int)memory.size(); i++) {
            int idx = dist(gen);
            auto& entry = memory[idx];
            std::vector<double> input = features_to_input(entry.features);
            int target = entry.actual_outcome > 0 ? 0 : 1;
            double reward = entry.pnl > 0 ? 1.2 : 0.6;
            train_step(input, target, reward);
        }
    }

    // Save model to file
    bool save(const std::string& path) {
        std::ofstream f(path, std::ios::binary);
        if (!f) return false;

        f.write(reinterpret_cast<char*>(W1), sizeof(W1));
        f.write(reinterpret_cast<char*>(b1), sizeof(b1));
        f.write(reinterpret_cast<char*>(W2), sizeof(W2));
        f.write(reinterpret_cast<char*>(b2), sizeof(b2));
        f.write(reinterpret_cast<char*>(&learning_rate), sizeof(learning_rate));
        f.write(reinterpret_cast<char*>(&total_trades), sizeof(total_trades));
        f.write(reinterpret_cast<char*>(&correct_predictions), sizeof(correct_predictions));
        f.write(reinterpret_cast<char*>(&total_pnl), sizeof(total_pnl));

        // Save digit counts
        f.write(reinterpret_cast<char*>(digit_counts), sizeof(digit_counts));
        f.write(reinterpret_cast<char*>(&digit_total), sizeof(digit_total));

        return true;
    }

    // Load model from file
    bool load(const std::string& path) {
        std::ifstream f(path, std::ios::binary);
        if (!f) return false;

        f.read(reinterpret_cast<char*>(W1), sizeof(W1));
        f.read(reinterpret_cast<char*>(b1), sizeof(b1));
        f.read(reinterpret_cast<char*>(W2), sizeof(W2));
        f.read(reinterpret_cast<char*>(b2), sizeof(b2));
        f.read(reinterpret_cast<char*>(&learning_rate), sizeof(learning_rate));
        f.read(reinterpret_cast<char*>(&total_trades), sizeof(total_trades));
        f.read(reinterpret_cast<char*>(&correct_predictions), sizeof(correct_predictions));
        f.read(reinterpret_cast<char*>(&total_pnl), sizeof(total_pnl));
        f.read(reinterpret_cast<char*>(digit_counts), sizeof(digit_counts));
        f.read(reinterpret_cast<char*>(&digit_total), sizeof(digit_total));

        return true;
    }

    // Get stats
    std::string get_stats() {
        double accuracy = total_trades > 0 ? (double)correct_predictions / total_trades * 100 : 0;
        std::ostringstream ss;
        ss << "{\"trades\":" << total_trades
           << ",\"accuracy\":" << accuracy
           << ",\"pnl\":" << total_pnl
           << ",\"learning_rate\":" << learning_rate
           << ",\"buffer\":" << price_buffer.size()
           << ",\"memory\":" << memory.size()
           << ",\"digit_total\":" << digit_total << "}";
        return ss.str();
    }

    // Get features as JSON
    std::string get_features_json() {
        FeatureVector fv = extract_features();
        std::ostringstream ss;
        ss << "{\"z_score\":" << fv.z_score
           << ",\"trend_slope\":" << fv.trend_slope
           << ",\"volatility\":" << fv.volatility
           << ",\"bb_position\":" << fv.bb_position
           << ",\"digit_bias\":" << fv.digit_bias
           << ",\"momentum\":" << fv.momentum
           << ",\"mean_reversion\":" << fv.mean_reversion
           << ",\"regime\":" << fv.regime << "}";
        return ss.str();
    }

    // Adjust learning rate based on performance
    void adapt_learning_rate() {
        if (total_trades < 10) return;
        double recent_accuracy = (double)correct_predictions / total_trades;
        if (recent_accuracy > 0.6) learning_rate = std::max(0.001, learning_rate * 0.99);
        else if (recent_accuracy < 0.4) learning_rate = std::min(0.1, learning_rate * 1.01);
    }
};

#endif
