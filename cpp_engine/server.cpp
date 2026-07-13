#include "market_engine.h"
#include <iostream>
#include <string>
#include <sstream>

/*
 * ALM-DR C++ Market Engine — Persistent Server Mode
 * Stays alive, accepts JSON commands via stdin, outputs JSON responses.
 * One engine instance, persistent memory, online learning.
 *
 * Commands (one JSON per line):
 *   {"cmd":"tick","price":12345.67,"epoch":1720000000}
 *   {"cmd":"predict"}
 *   {"cmd":"learn","profit":0.45,"stake":1.0}
 *   {"cmd":"features"}
 *   {"cmd":"train_batch","data":[[price,epoch,profit,stake],...]}
 *   {"cmd":"stats"}
 *   {"cmd":"save"}
 *   {"cmd":"quit"}
 */

static std::string parse_json_string(const std::string& json, const std::string& key) {
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return "";
    pos = json.find(':', pos + search.size());
    if (pos == std::string::npos) return "";
    pos++;
    while (pos < json.size() && json[pos] == ' ') pos++;
    if (json[pos] == '"') {
        size_t end = json.find('"', pos + 1);
        return json.substr(pos + 1, end - pos - 1);
    }
    size_t end = pos;
    while (end < json.size() && json[end] != ',' && json[end] != '}' && json[end] != ']') end++;
    return json.substr(pos, end - pos);
}

static double parse_json_number(const std::string& json, const std::string& key, double def = 0) {
    std::string val = parse_json_string(json, key);
    if (val.empty()) return def;
    try { return std::stod(val); } catch (...) { return def; }
}

static std::vector<std::vector<double>> parse_json_2d_array(const std::string& json, const std::string& key) {
    std::vector<std::vector<double>> result;
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return result;
    pos = json.find('[', pos);
    if (pos == std::string::npos) return result;

    // Find matching ]
    int depth = 0;
    size_t start = pos;
    for (; pos < json.size(); pos++) {
        if (json[pos] == '[') depth++;
        if (json[pos] == ']') depth--;
        if (depth == 0) break;
    }
    std::string array_str = json.substr(start, pos - start + 1);

    // Parse inner arrays
    size_t inner_start = array_str.find('[', 1);
    while (inner_start != std::string::npos) {
        size_t inner_end = array_str.find(']', inner_start);
        if (inner_end == std::string::npos) break;
        std::string inner = array_str.substr(inner_start + 1, inner_end - inner_start - 1);
        std::vector<double> row;
        std::string num;
        for (char c : inner) {
            if (c >= '0' && c <= '9' || c == '.' || c == '-') {
                num += c;
            } else if (!num.empty()) {
                row.push_back(std::stod(num));
                num = "";
            }
        }
        if (!num.empty()) row.push_back(std::stod(num));
        if (!row.empty()) result.push_back(row);
        inner_start = array_str.find('[', inner_end);
    }
    return result;
}

int main() {
    MarketEngine engine;
    std::string model_path = "alm_model.bin";
    engine.load(model_path);

    std::cerr << "[ALM-Engine] C++ Market Engine started. Waiting for commands on stdin..." << std::endl;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;

        std::string cmd = parse_json_string(line, "cmd");

        if (cmd == "tick") {
            double price = parse_json_number(line, "price");
            double epoch = parse_json_number(line, "epoch");
            engine.add_tick(price, epoch);
            TradeSignal sig = engine.predict();
            std::cout << "{\"signal\":" << sig.direction
                      << ",\"confidence\":" << sig.confidence
                      << ",\"ev\":" << sig.expected_value
                      << ",\"reason\":\"" << sig.reason << "\""
                      << "," << engine.get_stats().substr(1) << std::endl;
            std::cout.flush();
        }
        else if (cmd == "predict") {
            TradeSignal sig = engine.predict();
            std::cout << "{\"signal\":" << sig.direction
                      << ",\"confidence\":" << sig.confidence
                      << ",\"ev\":" << sig.expected_value
                      << ",\"reason\":\"" << sig.reason << "\"}" << std::endl;
            std::cout.flush();
        }
        else if (cmd == "learn") {
            double profit = parse_json_number(line, "profit");
            double stake = parse_json_number(line, "stake");
            engine.learn(profit, stake);
            engine.adapt_learning_rate();
            std::cout << "{\"status\":\"learned\"," << engine.get_stats().substr(1) << std::endl;
            std::cout.flush();
        }
        else if (cmd == "features") {
            std::cout << engine.get_features_json() << std::endl;
            std::cout.flush();
        }
        else if (cmd == "train_batch") {
            auto data = parse_json_2d_array(line, "data");
            int trained = 0;
            for (auto& row : data) {
                if (row.size() >= 4) {
                    engine.add_tick(row[0], row[1]);
                    engine.learn(row[3], row[2] > 0 ? row[2] : 1.0);
                    trained++;
                }
            }
            engine.adapt_learning_rate();
            std::cout << "{\"trained\":" << trained << "," << engine.get_stats().substr(1) << std::endl;
            std::cout.flush();
        }
        else if (cmd == "stats") {
            std::cout << engine.get_stats() << std::endl;
            std::cout.flush();
        }
        else if (cmd == "save") {
            engine.save(model_path);
            std::cout << "{\"saved\":true}" << std::endl;
            std::cout.flush();
        }
        else if (cmd == "quit") {
            engine.save(model_path);
            std::cout << "{\"status\":\"bye\"}" << std::endl;
            std::cout.flush();
            break;
        }
        else {
            std::cout << "{\"error\":\"unknown cmd: " << cmd << "\"}" << std::endl;
            std::cout.flush();
        }
    }

    engine.save(model_path);
    return 0;
}
