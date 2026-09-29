// JS8 reference harness for pluto-tx (J0 of docs/JS8_PLAN.md).
//
// Links the unmodified JS8Call sources (pinned tag, see docs/js8/SPEC.md)
// and exposes on the command line:
//
//   js8ref vectors   < cases.tsv    text -> frames -> flags -> 79 tones
//   js8ref decode FILE.wav MASK [nfqso]
//                                   offline decode of one 12 kHz/16-bit slot
//                                   (sample 0 = slot start); MASK bits:
//                                   1 Normal, 2 Fast, 4 Turbo, 8 Slow
//   js8ref dump-jsc                 the compiled JSC tables (cross-check for
//                                   tools/js8_extract_tables.py)
//
// Build: tools/js8ref/build.sh. The frame flags in `vectors` follow
// MainWindow::prepareNextMessageFrame() (JS8_UI/mainwindow.cpp): First only
// on the first frame, Last forced on the last one.
//
// cases.tsv: one case per line, TAB separated:
//   name  mycall  mygrid  selectedCall  text  submode  forceIdentify(0/1)  forceData(0/1)
//
// Output is JSON lines. GPLv3 like JS8Call.

#include "JS8_Include/commons.h"
#include "JS8_Main/varicode.h"
#include "JS8_Mode/JS8.h"
#include "JS8_Mode/JS8Submode.h"
#include "JS8_Mode/decodedtext.h"
#include "JS8_jsc/jsc.h"

#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextStream>
#include <QTimer>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <iostream>

struct dec_data dec_data;
struct specData specData;
std::mutex fftw_mutex;

// Defined in Decoder.cpp / mainwindow.cpp in JS8Call; not linked here.
#include <QLoggingCategory>
Q_LOGGING_CATEGORY(decoder_js8, "decoder.js8", QtWarningMsg)

namespace {

void emitJson(QJsonObject const &o) {
    std::cout << QJsonDocument(o).toJson(QJsonDocument::Compact).toStdString()
              << "\n";
    std::cout.flush();
}

int runVectors() {
    QTextStream in(stdin);
    while (!in.atEnd()) {
        QString line = in.readLine();
        if (line.trimmed().isEmpty() || line.startsWith('#'))
            continue;
        QStringList f = line.split('\t');
        if (f.size() < 8) {
            std::cerr << "bad line: " << line.toStdString() << "\n";
            return 2;
        }
        QString name = f[0], mycall = f[1], mygrid = f[2], sel = f[3],
                text = f[4];
        int submode = f[5].toInt();
        bool forceIdentify = f[6] == "1", forceData = f[7] == "1";

        Varicode::MessageInfo info;
        auto frames = Varicode::buildMessageFrames(
            mycall, mygrid, sel, text, forceIdentify, forceData, submode,
            &info);

        QJsonArray out;
        for (int i = 0; i < frames.size(); ++i) {
            QString frame = frames[i].first;
            int builderBits = frames[i].second;
            // Same adjustment as MainWindow::prepareNextMessageFrame().
            int bits = builderBits;
            if (i > 0)
                bits &= ~Varicode::JS8CallFirst;
            if (i == frames.size() - 1)
                bits |= Varicode::JS8CallLast;

            std::array<int, JS8_NUM_SYMBOLS> tones{};
            QByteArray msg = frame.toLatin1();
            JS8::encode(bits,
                        JS8::Costas::array(JS8::Submode::costas(submode)),
                        msg.constData(), tones.data());
            QString toneStr;
            for (int t : tones)
                toneStr += QString::number(t);

            DecodedText dt(frame, bits, submode);
            QJsonObject fo;
            fo["frame"] = frame;
            fo["builder_bits"] = builderBits;
            fo["bits"] = bits;
            fo["tones"] = toneStr;
            fo["frame_type"] = int(dt.frameType());
            fo["message"] = dt.message();
            out.append(fo);
        }
        QJsonObject o;
        o["case"] = name;
        o["mycall"] = mycall;
        o["mygrid"] = mygrid;
        o["selected"] = sel;
        o["text"] = text;
        o["submode"] = submode;
        o["force_identify"] = forceIdentify;
        o["force_data"] = forceData;
        o["dir_to"] = info.dirTo;
        o["dir_cmd"] = info.dirCmd;
        o["dir_num"] = info.dirNum;
        o["frames"] = out;
        emitJson(o);
    }
    return 0;
}

bool readWav(QString const &path, std::vector<std::int16_t> &samples) {
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly))
        return false;
    QByteArray all = f.readAll();
    if (all.size() < 44 || !all.startsWith("RIFF"))
        return false;
    // Walk the chunks; require PCM 16 bit mono 12000 Hz.
    int pos = 12;
    int rate = 0, bitsPerSample = 0, channels = 0;
    while (pos + 8 <= all.size()) {
        QByteArray id = all.mid(pos, 4);
        quint32 len;
        std::memcpy(&len, all.constData() + pos + 4, 4);
        if (id == "fmt ") {
            quint16 ch, bps;
            quint32 r;
            std::memcpy(&ch, all.constData() + pos + 10, 2);
            std::memcpy(&r, all.constData() + pos + 12, 4);
            std::memcpy(&bps, all.constData() + pos + 22, 2);
            channels = ch;
            rate = r;
            bitsPerSample = bps;
        } else if (id == "data") {
            if (rate != 12000 || bitsPerSample != 16 || channels != 1) {
                std::cerr << "need 12000 Hz mono 16 bit, got " << rate << " Hz "
                          << channels << " ch " << bitsPerSample << " bit\n";
                return false;
            }
            int n = std::min<int>(len, all.size() - pos - 8) / 2;
            samples.resize(n);
            std::memcpy(samples.data(), all.constData() + pos + 8, n * 2);
            return true;
        }
        pos += 8 + len + (len & 1);
    }
    return false;
}

} // namespace

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    if (argc >= 2 && std::strcmp(argv[1], "vectors") == 0)
        return runVectors();

    if (argc >= 2 && std::strcmp(argv[1], "dump-jsc") == 0) {
        // table <TAB> i <TAB> hex(str bytes up to NUL) <TAB> size <TAB> index
        auto dump = [](char const *name, Tuple const *t, quint32 n) {
            for (quint32 i = 0; i < n; ++i) {
                std::cout << name << '\t' << i << '\t'
                          << QByteArray(t[i].str).toHex().toStdString()
                          << '\t' << t[i].size << '\t' << t[i].index << '\n';
            }
        };
        dump("map", JSC::map, JSC::size);
        dump("list", JSC::list, JSC::size);
        dump("prefix", JSC::prefix, JSC::prefixSize);
        return 0;
    }

    if (argc >= 4 && std::strcmp(argv[1], "decode") == 0) {
        std::vector<std::int16_t> samples;
        if (!readWav(argv[2], samples)) {
            std::cerr << "cannot read " << argv[2] << "\n";
            return 2;
        }
        int mask = std::atoi(argv[3]);
        std::memset(&dec_data, 0, sizeof dec_data);
        int n = std::min<int>(samples.size(), JS8_RX_SAMPLE_SIZE);
        std::memcpy(dec_data.d2, samples.data(), n * sizeof(std::int16_t));
        auto &p = dec_data.params;
        p.nutc = 0;
        p.nfqso = argc >= 5 ? std::atoi(argv[4]) : 1500;
        p.newdat = true;
        p.nfa = 0;
        p.nfb = 5000;
        p.syncStats = false;
        p.kin = n;
        int const period[5] = {15, 10, 6, 30, 4};
        int *kpos[5] = {&p.kposA, &p.kposB, &p.kposC, &p.kposE, &p.kposI};
        int *ksz[5] = {&p.kszA, &p.kszB, &p.kszC, &p.kszE, &p.kszI};
        for (int i = 0; i < 5; ++i) {
            *kpos[i] = 0;
            *ksz[i] = std::min(n, period[i] * JS8_RX_SAMPLE_RATE);
        }
        p.nsubmodes = mask;

        auto *decoder = new JS8::Decoder(&app);
        auto t0 = std::make_shared<std::chrono::steady_clock::time_point>();
        QObject::connect(
            decoder, &JS8::Decoder::decodeEvent, &app,
            [&app, decoder, t0](JS8::Event::Variant const &ev) {
                if (auto d = std::get_if<JS8::Event::Decoded>(&ev)) {
                    // Decoded.mode is Mode::NSUBMODE, i.e. the Varicode
                    // submode number (0 Normal, 1 Fast, 2 Turbo, 4 Slow).
                    int sm = d->mode;
                    DecodedText dt(*d);
                    QJsonObject o;
                    o["snr"] = d->snr;
                    o["dt"] = d->xdt;
                    o["freq"] = d->frequency;
                    o["frame"] = QString::fromStdString(d->data);
                    o["bits"] = d->type;
                    o["quality"] = d->quality;
                    o["submode"] = sm;
                    o["frame_type"] = int(dt.frameType());
                    o["message"] = dt.message();
                    emitJson(o);
                } else if (auto f =
                               std::get_if<JS8::Event::DecodeFinished>(&ev)) {
                    auto ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - *t0)
                                  .count();
                    QJsonObject o;
                    o["finished"] = int(f->decoded);
                    o["ms"] = ms;
                    emitJson(o);
                    decoder->quit();
                    app.quit();
                }
            });
        decoder->start(QThread::NormalPriority);
        QTimer::singleShot(0, [decoder, t0] {
            *t0 = std::chrono::steady_clock::now();
            decoder->decode();
        });
        return app.exec();
    }

    std::cerr << "usage: js8ref vectors < cases.tsv\n"
                 "       js8ref decode FILE.wav MASK [nfqso]\n"
                 "MASK bits: 1 Normal, 2 Fast, 4 Turbo, 8 Slow\n";
    return 1;
}
