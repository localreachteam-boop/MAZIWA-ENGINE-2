#include "market_engine.h"
#include <iostream>
#include <string>
#include <sstream>

/*
 * ALM-DR C++ Market Engine
 * Standalone inference + online training for market prediction.
 *
 * Usage:
 *   ./engine tick <price> <epoch>           - Add tick, get prediction
 *   ./engine predict                        - Get current prediction
 *   ./engine learn <profit> <stake>         - Learn from trade result
 *   ./engine features                       - Get current features
 *   ./engine save <path>                    - Save model
 *   ./engine load <path>                    - Load model
 *   ./engine stats                          - Get engine stats
 *   ./engine batch <ticks_json>             - Add multiple ticks
 *   ./engine train_batch <data_json>        - Train on historical data
 */

int main(int argc, char* argv[]) {
    static MarketEngine engine;
    static const std::string MODEL_PATH = "alm_model.bin";

    // Auto-load saved model
    engine.load(MODEL_PATH);

    if (argc < 2) {
        std::cout << "{\"error\":\"usage: engine <command> [args]\"}" << std::endl;
        return 1;
    }

    std::string cmd = argv[1];

    if (cmd == "tick" && argc >= 4) {
        // Add tick and predict
        double price = std::stod(argv[2]);
        double epoch = std::stod(argv[3]);
        engine.add_tick(price, epoch);
        TradeSignal sig = engine.predict();

        std::cout << "{\"signal\":" << sig.direction
                  << ",\"confidence\":" << sig.confidence
                  << ",\"ev\":" << sig.expected_value
                  << ",\"reason\":\"" << sig.reason << "\""
                  << "," << engine.get_stats().substr(1) << std::endl;
    }
    else if (cmd == "predict") {
        TradeSignal sig = engine.predict();
        std::cout << "{\"signal\":" << sig.direction
                  << ",\"confidence\":" << sig.confidence
                  << ",\"ev\":" << sig.expected_value
                  << ",\"reason\":\"" << sig.reason << "\"}" << std::endl;
    }
    else if (cmd == "learn" && argc >= 4) {
        double profit = std::stod(argv[2]);
        double stake = std::stod(argv[3]);
        engine.learn(profit, stake);
        engine.adapt_learning_rate();
        engine.save(MODEL_PATH);
        std::cout << "{\"status\":\"learned\"," << engine.get_stats().substr(1) << std::endl;
    }
    else if (cmd == "features") {
        std::cout << engine.get_features_json() << std::endl;
    }
    else if (cmd == "save" && argc >= 3) {
        bool ok = engine.save(argv[2]);
        std::cout << "{\"saved\":" << (ok ? "true" : "false") << "}" << std::endl;
    }
    else if (cmd == "load" && argc >= 3) {
        bool ok = engine.load(argv[2]);
        std::cout << "{\"loaded\":" << (ok ? "true" : "false") << "}" << std::endl;
    }
    else if (cmd == "stats") {
        std::cout << engine.get_stats() << std::endl;
    }
    else if (cmd == "batch" && argc >= 3) {
        // Parse JSON array of ticks: [[price, epoch], [price, epoch], ...]
        std::string json = argv[2];
        // Simple parser: extract numbers
        std::vector<double> numbers;
        std::string num;
        for (char c : json) {
            if (c >= '0' && c <= '9' || c == '.' || c == '-') {
                num += c;
            } else if (!num.empty()) {
                numbers.push_back(std::stod(num));
                num = "";
            }
        }
        if (!num.empty()) numbers.push_back(std::stod(num));

        for (size_t i = 0; i + 1 < numbers.size(); i += 2) {
            engine.add_tick(numbers[i], numbers[i + 1]);
        }
        TradeSignal sig = engine.predict();
        std::cout << "{\"signal\":" << sig.direction
                  << ",\"confidence\":" << sig.confidence
                  << ",\"ev\":" << sig.expected_value
                  << ",\"reason\":\"" << sig.reason << "\""
                  << "," << engine.get_stats().substr(1) << std::endl;
    }
    else if (cmd == "train_batch" && argc >= 3) {
        // Train on historical data: [[price, epoch, outcome, pnl], ...]
        std::string json = argv[2];
        std::vector<double> numbers;
        std::string num;
        for (char c : json) {
            if (c >= '0' && c <= '9' || c == '.' || c == '-') {
                num += c;
            } else if (!num.empty()) {
                numbers.push_back(std::stod(num));
                num = "";
            }
        }
        if (!num.empty()) numbers.push_back(std::stod(num));

        int trained = 0;
        for (size_t i = 0; i + 3 < numbers.size(); i += 4) {
            engine.add_tick(numbers[i], numbers[i + 1]);
            // numbers[i+2] = outcome (unused, we derive from price)
            engine.learn(numbers[i + 3], 1.0); // pnl, stake
            trained++;
        }
        engine.adapt_learning_rate();
        engine.save(MODEL_PATH);
        std::cout << "{\"trained\":" << trained << "," << engine.get_stats().substr(1) << std::endl;
    }
    else {
        std::cout << "{\"error\":\"unknown command: " << cmd << "\"}" << std::endl;
        return 1;
    }

    return 0;
}
