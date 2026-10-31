// Generates tests/data/adsl/vectors.jsonl from the CC0 reference code of github.com/pjalocha/ADS-L
// (commit 07f24bd, directory Air2Air-Position). Not part of the build; the JSONL output is what is committed.
//   g++ -O2 -I<ADS-L>/Air2Air-Position gen_vectors.cc <ADS-L>/Air2Air-Position/ognconv.cpp \
//       <ADS-L>/Air2Air-Position/format.cpp -o gen_vectors && ./gen_vectors > vectors.jsonl
#include <stdio.h>
#include <stdint.h>
#include "adsl.h"

static uint32_t Seed = 0x2A5F19C3;
static uint32_t Next(void) { XorShift32(Seed); return Seed; }
static int32_t Range(int32_t Min, int32_t Max) { return Min + (int32_t)(Next() % (uint32_t)(Max - Min + 1)); }

static void Emit(ADSL_Packet &P)
{ char Clear[64]; int Len = 0;
  for (int Idx = 0; Idx < 20; Idx++) Len += sprintf(Clear + Len, "%02x", P.Byte[Idx]);
  const uint8_t Type = P.Type, Table = P.getAddrTable(), State = P.FlightState, Cat = P.AcftCat, Emerg = P.Emergency;
  const uint32_t Addr = P.getAddress();
  const int Time = P.TimeStamp, Relay = P.isRelay();
  const double Lat = P.FNTtoFloat(P.getLat()), Lon = P.FNTtoFloat(P.getLon());
  const int SpeedRaw = P.Position[6], AltRaw = (P.Position[8] & 0x3F) << 8 | P.Position[7];
  const int ClimbRaw = P.getClimbWord(), Track = P.getTrack();
  const int Speed = P.getSpeed(), Alt = P.getAlt(), Climb = P.getClimb();
  P.Scramble(); P.setCRC();
  printf("{\"frame\":\"");
  for (int Idx = 2; Idx < ADSL_Packet::TxBytes; Idx++) printf("%02x", ((const uint8_t *)&P)[Idx]);
  printf("\",\"clear\":\"%s\",\"check\":%u,\"type\":%d,\"table\":%d,\"address\":%u,\"relay\":%d,\"timestamp\":%d,"
         "\"flight_state\":%d,\"category\":%d,\"emergency\":%d,\"lat\":%.7f,\"lon\":%.7f,\"speed_raw\":%d,"
         "\"alt_raw\":%d,\"climb_raw\":%d,\"track_raw\":%d,\"ref_speed_qms\":%d,\"ref_alt_m\":%d,\"ref_climb_8ms\":%d}\n",
         Clear, (unsigned)P.checkCRC(), Type, Table, Addr, Relay, Time, State, Cat, Emerg, Lat, Lon, SpeedRaw,
         AltRaw, ClimbRaw, Track, Speed, Alt, Climb);
}

int main(void)
{ static ADSL_Packet P;
  // the reference's own adsl_test.cc example
  P.Init(); P.setAddress(0x123456); P.setAddrTable(8); P.TimeStamp = 4 * 4;
  P.setLat(P.UBXtoFNT(481234500)); P.setLon(P.UBXtoFNT(81234500));
  P.setAlt(1234); P.setSpeed(45 * 4); P.setClimb(-5 * 8 - 4); P.setTrack(0x100);
  Emit(P);
  for (int Idx = 0; Idx < 200; Idx++)
  { P.Init();
    P.setAddress(Next() & 0xFFFFFF);
    static const uint8_t Tables[] = { 0, 5, 6, 7, 8, 9, 42 };
    P.setAddrTable(Tables[Next() % sizeof(Tables)]);
    P.setRelay(Next() % 8 == 0);
    P.TimeStamp = Range(0, 59); P.FlightState = Range(0, 2); P.AcftCat = Range(0, 17); P.Emergency = Range(0, 6);
    P.setLat(P.UBXtoFNT(Range(-899000000, 899000000))); P.setLon(P.UBXtoFNT(Range(-1799000000, 1799000000)));
    P.setAlt(Range(-300, 20000)); P.setSpeed(Range(0, 900)); P.setClimb(Range(-900, 900)); P.setTrack(Range(0, 511));
    P.SourceIntegrity = Range(0, 3); P.DesignAssurance = Range(0, 3); P.NavigIntegrity = Range(0, 12);
    P.HorizAccuracy = Range(0, 7); P.VertAccuracy = Range(0, 3); P.VelAccuracy = Range(0, 3);
    Emit(P); }
  return 0; }
