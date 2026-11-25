"""Real FAA NASR rows, 2026-10-29 cycle, for four landing facilities in BOTH formats
(the CSV tables and the fixed-width APT.txt / ILS.txt), cut verbatim from
28DaySubscription_Effective_2026-10-29.zip. Data for test_nasr_csv.py; no tests here.

  KTMB  Miami Exec     LED remark with an unpadded runway and no full stop ("RWY 9R");
                       a 75 ft helipad beside three runways
  KPOB  Pope AAF       the non-standard LED wording, a runway pair, a 3,000 ft assault strip
  KILN  Wilmington     two decommissioned ILS (04R, 22L) that ILS.txt still lists
  7NY9  a heliport     LED lights on its "APCH/DEP PATHS": not approach lights

RWY lines are whole (1,531 characters). APT lines keep their length and the
fields the reader uses; the rest is blanked, because it names the facility's
owner and manager with addresses and telephone numbers. RMK and ILS lines have
their trailing blanks cut, and only ILS1/ILS2 records are kept; of each
facility's remarks only the LED ones and one other are kept; the layout lines
are the ones build_chase.apt_layout() looks up.

To cut the same facilities from another cycle (a directory laid out as
fetch_nasr.py leaves data/nasr/):   python pipeline/test_nasr_fixture.py DIR
"""
import csv
import io
import re
import sys
from pathlib import Path

FACILITIES = ("TMB", "POB", "ILN", "7NY9")

# ---- data: generated, do not edit by hand -----------------------------------
CYCLE = '2026-10-29'

CSV = {
    'APT_BASE.csv': (
        'EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,REGION_CODE,ADO_CODE,STATE_NAME,COUNTY_NAME,COUNTY_ASSOC_STATE,ARPT_NAME,OWNERSHIP_TYPE_CODE,FACILITY_USE_CODE,LAT_DEG,LAT_MIN,LAT_SEC,LAT_HEMIS,LAT_DECIMAL,LONG_DEG,LONG_MIN,LONG_SEC,LONG_HEMIS,LONG_DECIMAL,SURVEY_METHOD_CODE,ELEV,ELEV_METHOD_CODE,MAG_VARN,MAG_HEMIS,MAG_VARN_YEAR,TPA,CHART_NAME,DIST_CITY_TO_AIRPORT,DIRECTION_CODE,ACREAGE,RESP_ARTCC_ID,COMPUTER_ID,ARTCC_NAME,FSS_ON_ARPT_FLAG,FSS_ID,FSS_NAME,PHONE_NO,TOLL_FREE_NO,ALT_FSS_ID,ALT_FSS_NAME,ALT_TOLL_FREE_NO,NOTAM_ID,NOTAM_FLAG,ACTIVATION_DATE,ARPT_STATUS,FAR_139_TYPE_CODE,FAR_139_CARRIER_SER_CODE,ARFF_CERT_TYPE_DATE,NASP_CODE,ASP_ANLYS_DTRM_CODE,CUST_FLAG,LNDG_RIGHTS_FLAG,JOINT_USE_FLAG,MIL_LNDG_FLAG,INSPECT_METHOD_CODE,INSPECTOR_CODE,LAST_INSPECTION,LAST_INFO_RESPONSE,FUEL_TYPES,AIRFRAME_REPAIR_SER_CODE,PWR_PLANT_REPAIR_SER,BOTTLED_OXY_TYPE,BULK_OXY_TYPE,LGT_SKED,BCN_LGT_SKED,TWR_TYPE_CODE,SEG_CIRCLE_MKR_FLAG,BCN_LENS_COLOR,LNDG_FEE_FLAG,MEDICAL_USE_FLAG,ARPT_PSN_SOURCE,POSITION_SRC_DATE,ARPT_ELEV_SOURCE,ELEVATION_SRC_DATE,CONTR_FUEL_AVBL,TRNS_STRG_BUOY_FLAG,TRNS_STRG_HGR_FLAG,TRNS_STRG_TIE_FLAG,OTHER_SERVICES,WIND_INDCR_FLAG,ICAO_ID,MIN_OP_NETWORK,USER_FEE_FLAG,CTA\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,ASO,ORL,FLORIDA,DADE,FL,MIAMI EXEC,PU,PU,25,38,51.2294,N,25.64756372,80,25,59.6086,W,-80.43322461,E,10,S,6,W,2010,,MIAMI,13,SW,1380,ZMA,ZCR,MIAMI,N,MIA,MIAMI,,1-800-WX-BRIEF,,,,TMB,Y,1967/11,O,,,,NGY,NO OBJECTION,N,Y,N,Y,S,S,2026/05/07,,"100LL,A",MAJOR,MAJOR,HIGH/LOW,LOW,SEE RMK,SS-SR,ATCT,Y-L,WG,N,,FAA-EST,2012/02/29,FAA,2012/02/28,,N,Y,Y,"AMB,AVNCS,CHTR,INSTR,RNTL,SALES,SURV",Y-L,KTMB,Y,,\n'
        '2026/10/29,16476.,H,NY,7NY9,YORKTOWN,US,AEA,,NEW YORK,WESTCHESTER,NY,YORKTOWN HELIPAD,PR,PR,41,12,43,N,41.21194444,73,48,12,W,-73.80333333,E,499,E,,,,,,,,,ZNY,ZCN,NEW YORK,,ISP,NEW YORK,,1-800-WX-BRIEF,,,,,,2013/12,O,,,,,,,,,,,N,,2020/01/20,,,,,,SEE RMK,,NON-ATCT,,,,,FAA,2025/04/22,FAA,2025/04/22,,,,,,Y-L,,N,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,ASO,MEM,NORTH CAROLINA,CUMBERLAND,NC,POPE AAF,MR,PR,35,10,15.201,N,35.17088916,79,0,52.188,W,-79.01449666,E,218,E,7,W,1985,,CHARLOTTE,12,NW,,ZDC,ZCW,WASHINGTON,N,RDU,RALEIGH,,1-800-WX-BRIEF,,,,POB,Y,,O,,,,,NOT ANALYZED,N,N,,N,,N,,2013/01/09,"A,A++",MINOR,MINOR,NONE,LOW,,SS-SR,ATCT,N,WG,,,FAA,2013/03/22,MILITARY,2018/04/11,,,Y,,CARGO,Y,KPOB,N,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,AGL,DET,OHIO,CLINTON,OH,WILMINGTON AIR PARK,PU,PU,39,25,40.5,N,39.42791666,83,47,31.6,W,-83.79211111,E,1076.7,S,7,W,2025,,CINCINNATI,2,SE,2000,ZID,ZCI,INDIANAPOLIS,N,DAY,DAYTON,,1-800-WX-BRIEF,,,,ILN,Y,1972/09,O,IV A,U,1974/10,,NO OBJECTION,N,N,N,N,F,F,2018/03/23,,A,MAJOR,MAJOR,NONE,HIGH/LOW,SEE RMK,SS-SR,ATCT,N,WG,Y,,3RD PARTY SURVEY,2007/05/21,3RD PARTY SURVEY,2007/05/21,,N,,,,Y-L,KILN,N,,\n'
    ),
    'APT_RWY.csv': (
        'EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,RWY_ID,RWY_LEN,RWY_WIDTH,SURFACE_TYPE_CODE,COND,TREATMENT_CODE,PAVEMENT_CLASSIFICATION,PCN_PCR_NUMBER,PAVEMENT_TYPE_CODE,SUBGRADE_STRENGTH_CODE,TIRE_PRES_CODE,DTRM_METHOD_CODE,RWY_LGT_CODE,RWY_LEN_SOURCE,LENGTH_SOURCE_DATE,GROSS_WT_SW,GROSS_WT_DW,GROSS_WT_DTW,GROSS_WT_DDTW\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09L/27R,5003,150,ASPH,FAIR,GRVD,PCN,32,F,A,Y,T,MED,NGS,2010/02/07,87,135,195,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09R/27L,6000,150,ASPH,FAIR,GRVD,PCN,32,F,A,Y,T,HIGH,FAA,2014/11/07,87,135,195,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,13/31,4001,150,ASPH,EXCELLENT,GRVD,PCN,32,F,A,Y,T,MED,NGS,2010/02/07,87,135,195,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,H1,75,75,ASPH,FAIR,NONE,,,,,,,,,,,,,\n'
        '2026/10/29,16476.,H,NY,7NY9,YORKTOWN,US,H1,100,100,TURF,,NONE,,,,,,,HIGH,OWNER,2013/10/01,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,05/23,7501,150,CONC,GOOD,GRVD,PCN,93,R,B,W,T,HIGH,MILITARY,2003/07/10,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,051/231,3000,60,ASPH,,NONE,PCN,44,F,A,W,T,LOW,MILITARY,2003/07/10,,,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04L/22R,10701,150,CONC,FAIR,GRVD,PCR,920,R,C,W,T,HIGH,3RD PARTY SURVEY,2007/05/21,100,200,390,750\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04R/22L,9000,150,CONC,FAIR,GRVD,PCR,900,R,D,W,T,HIGH,3RD PARTY SURVEY,2007/05/21,100,200,390,750\n'
    ),
    'APT_RWY_END.csv': (
        'EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,RWY_ID,RWY_END_ID,TRUE_ALIGNMENT,ILS_TYPE,RIGHT_HAND_TRAFFIC_PAT_FLAG,RWY_MARKING_TYPE_CODE,RWY_MARKING_COND,RWY_END_LAT_DEG,RWY_END_LAT_MIN,RWY_END_LAT_SEC,RWY_END_LAT_HEMIS,LAT_DECIMAL,RWY_END_LONG_DEG,RWY_END_LONG_MIN,RWY_END_LONG_SEC,RWY_END_LONG_HEMIS,LONG_DECIMAL,RWY_END_ELEV,THR_CROSSING_HGT,VISUAL_GLIDE_PATH_ANGLE,DISPLACED_THR_LAT_DEG,DISPLACED_THR_LAT_MIN,DISPLACED_THR_LAT_SEC,DISPLACED_THR_LAT_HEMIS,LAT_DISPLACED_THR_DECIMAL,DISPLACED_THR_LONG_DEG,DISPLACED_THR_LONG_MIN,DISPLACED_THR_LONG_SEC,DISPLACED_THR_LONG_HEMIS,LONG_DISPLACED_THR_DECIMAL,DISPLACED_THR_ELEV,DISPLACED_THR_LEN,TDZ_ELEV,VGSI_CODE,RWY_VISUAL_RANGE_EQUIP_CODE,RWY_VSBY_VALUE_EQUIP_FLAG,APCH_LGT_SYSTEM_CODE,RWY_END_LGTS_FLAG,CNTRLN_LGTS_AVBL_FLAG,TDZ_LGT_AVBL_FLAG,OBSTN_TYPE,OBSTN_MRKD_CODE,FAR_PART_77_CODE,OBSTN_CLNC_SLOPE,OBSTN_HGT,DIST_FROM_THR,CNTRLN_OFFSET,CNTRLN_DIR_CODE,RWY_GRAD,RWY_GRAD_DIRECTION,RWY_END_PSN_SOURCE,RWY_END_PSN_DATE,RWY_END_ELEV_SOURCE,RWY_END_ELEV_DATE,DSPL_THR_PSN_SOURCE,RWY_END_DSPL_THR_PSN_DATE,DSPL_THR_ELEV_SOURCE,RWY_END_DSPL_THR_ELEV_DATE,TDZ_ELEV_SOURCE,RWY_END_TDZ_ELEV_DATE,TKOF_RUN_AVBL,TKOF_DIST_AVBL,ACLT_STOP_DIST_AVBL,LNDG_DIST_AVBL,LAHSO_ALD,RWY_END_INTERSECT_LAHSO,LAHSO_DESC,LAHSO_LAT,LAT_LAHSO_DECIMAL,LAHSO_LONG,LONG_LAHSO_DECIMAL,LAHSO_PSN_SOURCE,RWY_END_LAHSO_PSN_DATE\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09L/27R,09L,87,,N,NPI,FAIR,25,39,9.4872,N,25.65263533,80,26,24.3015,W,-80.44008375,7.8,53,3,,,,,,,,,,,,,8.2,P4L,,,,,,,,,D,50,,,,,,,NGS,2010/02/07,NGS,2010/02/07,,,,,AVN,2011/04/11,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09L/27R,27R,267,,Y,NPI,FAIR,25,39,11.8051,N,25.65327919,80,25,29.6936,W,-80.42491488,7.8,40,3,,,,,,,,,,,,,8.1,P2L,,,,,,,TOWER,LM,D,50,85,4453,100,R,,,NGS,2010/02/07,NGS,2010/02/07,,,,,AVN,2011/04/11,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09R/27L,09R,87,ILS,Y,PIR,FAIR,25,38,34.443,N,25.64290083,80,26,27.943,W,-80.44109527,10,55,3,,,,,,,,,,,,,10,P4R,,,MALSR,,,,,,PIR,50,,,,,,,FAA,2012/02/29,FAA,2012/02/28,,,,,FAA,2012/02/28,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09R/27L,27L,267,,N,PIR,FAIR,25,38,37.2256,N,25.64367377,80,25,22.464,W,-80.42290666,7.9,53,3,,,,,,,,,,,,,8.1,P4L,,,,,,,,,D,50,,,,,,,NGS,2010/02/07,NGS,2010/02/07,,,,,AVN,2011/04/11,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,13/31,13,127,,N,BSC,GOOD,25,39,2.0539,N,25.65057052,80,26,26.8558,W,-80.44079327,8,40,3,,,,,,,,,,,,,8.1,P4L,,,,Y,,,,,B(V),50,,,,,,,NGS,2010/02/07,NGS,2010/02/07,,,,,AVN,2011/04/11,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,13/31,31,307,,N,BSC,GOOD,25,38,38.0122,N,25.64389227,80,25,52.1011,W,-80.43113919,7.8,,,,,,,,,,,,,,,8.1,,,,,,,,POLE,,B(V),36,29,1215,250,L,,,NGS,2010/02/07,NGS,2010/02/07,,,,,AVN,2011/04/11,,,,,,,,,,,,,\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,H1,H1,,,,,GOOD,25,39,4.816,N,25.65133777,80,25,12.562,W,-80.42015611,8.6,,,,,,,,,,,,,,,,,,,,,,,,,A(V),20,,,,,,,OWNER,2016/07/15,OWNER,2016/07/15,,,,,,,,,,,,,,,,,,,\n'
        '2026/10/29,16476.,H,NY,7NY9,YORKTOWN,US,H1,H1,,,,,,41,12,43,N,41.21194444,73,48,12,W,-73.80333333,499,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,FAA,2025/04/22,FAA,2025/04/22,,,,,,,,,,,,,,,,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,05/23,05,41,,N,PIR,GOOD,35,9,47.4,N,35.16316666,79,1,22.09,W,-79.02280277,217.6,54,3,,,,,,,,,,,,,217.6,P4L,T,N,,Y,N,N,,,,,,,,,,,MILITARY,2013/03/22,MILITARY,2013/03/22,,,,,MILITARY,2013/03/22,,,,7501,,,,,,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,05/23,23,221,ILS,Y,PIR,GOOD,35,10,43,N,35.17861111,79,0,22.28,W,-79.00618888,190.7,54,3,,,,,,,,,,,,,200,P4L,T,N,ALSF1,N,N,N,,,,,,,,,,,MILITARY,2013/03/22,MILITARY,2013/03/22,,,,,MILITARY,2014/10/09,,,,7501,,,,,,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,051/231,051,,,Y,NPI,GOOD,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,051/231,231,,,N,NPI,GOOD,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04L/22R,04L,37,ILS,N,PIR,GOOD,39,25,9.6841,N,39.41935669,83,48,33.3907,W,-83.80927519,1053,62,3,,,,,,,,,,,,,1056.6,P4L,TMR,,MALSR,,Y,,,,PIR,50,,,,,,,3RD PARTY SURVEY,2007/05/21,3RD PARTY SURVEY,2007/05/21,,,,,3RD PARTY SURVEY,2007/05/21,10701,10701,10701,10701,,,,,,,,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04L/22R,22R,217,ILS,N,PIR,GOOD,39,26,33.7951,N,39.44272086,83,47,10.7292,W,-83.78631366,1076.7,71,3,,,,,,,,,,,,,1076.7,P4L,TMR,,ALSF2,,Y,Y,,,PIR,50,,,,,,,3RD PARTY SURVEY,2007/05/21,3RD PARTY SURVEY,2007/05/21,,,,,3RD PARTY SURVEY,2007/05/21,10701,10701,10701,10701,,,,,,,,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04R/22L,04R,37,,,PIR,GOOD,39,24,51.7868,N,39.41438522,83,47,42.0864,W,-83.795024,1042.5,71,3,,,,,,,,,,,,,1045.5,P4L,TMR,,,,Y,,,,PIR,50,,,,,,,3RD PARTY SURVEY,2007/05/21,3RD PARTY SURVEY,2007/05/21,,,,,3RD PARTY SURVEY,2007/05/21,9000,9000,9000,9000,,,,,,,,,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04R/22L,22L,217,,,PIR,GOOD,39,26,2.5263,N,39.43403508,83,46,32.5591,W,-83.77571086,1076.5,71,3,,,,,,,,,,,,,1076.5,P4L,TMR,,ALSF2,,Y,Y,,,PIR,50,,,,,,,3RD PARTY SURVEY,2007/05/21,3RD PARTY SURVEY,2007/05/21,,,,,3RD PARTY SURVEY,2007/05/21,9000,9000,9000,9000,,,,,,,,,\n'
    ),
    'APT_RMK.csv': (
        'EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,LEGACY_ELEMENT_NUMBER,TAB_NAME,REF_COL_NAME,ELEMENT,REF_COL_SEQ_NO,REMARK\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,A110-17,AIRPORT,GENERAL_REMARK,,17,PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 9R\n'
        '2026/10/29,16476.,H,NY,7NY9,YORKTOWN,US,A110-1,AIRPORT,GENERAL_REMARK,,1,"TWO APCH/DEP PATHS; ONE NORTH & ONE WEST, EACH DIRECTION LIGHTED BT FIVE (5) AMBER LED LIGHTS."\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,A110-23,AIRPORT,GENERAL_REMARK,,23,CAUTION: ALL TWY EDGE LGT AND RWY 05/23 EDGE AND APCH LGT EQUIPPED WITH NSTD LED FIXTURES. LED FIXTURES MAY NOT BE COMPATIBLE WITH ALL NVD OR ENHANCED FLIGHT VISION SYSTEMS.\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,A110-1,AIRPORT,GENERAL_REMARK,,1,ARPT CLSD TO NON-ENG ACFT.\n'
    ),
    'ILS_BASE.csv': (
        'EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,RWY_END_ID,ILS_LOC_ID,SYSTEM_TYPE_CODE,STATE_NAME,REGION_CODE,RWY_LEN,RWY_WIDTH,CATEGORY,OWNER,OPERATOR,APCH_BEAR,MAG_VAR,MAG_VAR_HEMIS,COMPONENT_STATUS,COMPONENT_STATUS_DATE,LAT_DEG,LAT_MIN,LAT_SEC,LAT_HEMIS,LAT_DECIMAL,LONG_DEG,LONG_MIN,LONG_SEC,LONG_HEMIS,LONG_DECIMAL,LAT_LONG_SOURCE_CODE,SITE_ELEVATION,LOC_FREQ,BK_COURSE_STATUS_CODE\n'
        '2026/10/29,03325.1,A,FL,TMB,MIAMI,US,09R,TMB,LS,FLORIDA,ASO,6000,150,I,F-FEDERAL AVIATION ADMIN,F-FEDERAL AVIATION ADMIN,93.31,6,W,OPERATIONAL IFR,1980/10/02,25,38,38.0402,N,25.64390005,80,25,2.9849,W,-80.4174958,T,8,108.7,\n'
        '2026/10/29,16723.03,A,NC,POB,FAYETTEVILLE,US,23,POB,LS,NORTH CAROLINA,ASO,7501,150,I,A-U.S. AIR FORCE,A-U.S. AIR FORCE,228,7,W,OPERATIONAL IFR,1901/01/01,35,9,32.2,N,35.15894444,79,1,38.44,W,-79.02734444,K,234.4,110.9,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,04L,HWM,LS,OHIO,AGL,10701,150,I,O-CLINTON COUNTY PORT AUTHORITY,O-CLINTON COUNTY PORT AUTHORITY,44.31,7,W,OPERATIONAL IFR,2026/01/27,39,26,42.18,N,39.44505,83,47,2.52,W,-83.78403333,F,1076.1,110.7,\n'
        '2026/10/29,18717.,A,OH,ILN,WILMINGTON,US,22R,ILN,LS,OHIO,AGL,10701,150,III,O-CLINTON COUNTY PORT AUTHORITY,O-CLINTON COUNTY PORT AUTHORITY,224.32,7,W,OPERATIONAL IFR,1982/03/18,39,25,0.8401,N,39.41690002,83,48,42.0796,W,-83.81168877,T,1053.1,110.7,\n'
    ),
}

APT_CSV_DATA_STRUCTURE = (
    'CSV File,Column Name,Max Length,Data Type,Nullable\n'
    'APT_RWY_END,EFF_DATE,10,VARCHAR,No\n'
    'APT_RWY_END,SITE_NO,9,VARCHAR,No\n'
    'APT_RWY_END,SITE_TYPE_CODE,1,VARCHAR,No\n'
    'APT_RWY_END,STATE_CODE,2,VARCHAR,Yes\n'
    'APT_RWY_END,ARPT_ID,4,VARCHAR,No\n'
    'APT_RWY_END,CITY,40,VARCHAR,No\n'
    'APT_RWY_END,COUNTRY_CODE,2,VARCHAR,No\n'
    'APT_RWY_END,RWY_ID,7,VARCHAR,No\n'
    'APT_RWY_END,RWY_END_ID,3,VARCHAR,No\n'
    'APT_RWY_END,TRUE_ALIGNMENT,"(3,0)",NUMBER,Yes\n'
    'APT_RWY_END,ILS_TYPE,10,VARCHAR,Yes\n'
    'APT_RWY_END,RIGHT_HAND_TRAFFIC_PAT_FLAG,1,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_MARKING_TYPE_CODE,4,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_MARKING_COND,4,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_LAT_DEG,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LAT_MIN,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LAT_SEC,"(6,4)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LAT_HEMIS,1,VARCHAR,Yes\n'
    'APT_RWY_END,LAT_DECIMAL,"(10,8)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LONG_DEG,"(3,0)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LONG_MIN,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LONG_SEC,"(6,4)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_LONG_HEMIS,1,VARCHAR,Yes\n'
    'APT_RWY_END,LONG_DECIMAL,"(11,8)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_ELEV,"(6,1)",NUMBER,Yes\n'
    'APT_RWY_END,THR_CROSSING_HGT,"(3,0)",NUMBER,Yes\n'
    'APT_RWY_END,VISUAL_GLIDE_PATH_ANGLE,"(3,2)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LAT_DEG,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LAT_MIN,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LAT_SEC,"(6,4)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LAT_HEMIS,1,VARCHAR,Yes\n'
    'APT_RWY_END,LAT_DISPLACED_THR_DECIMAL,"(10,8)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LONG_DEG,"(3,0)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LONG_MIN,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LONG_SEC,"(6,4)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LONG_HEMIS,1,VARCHAR,Yes\n'
    'APT_RWY_END,LONG_DISPLACED_THR_DECIMAL,"(11,8)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_ELEV,"(6,1)",NUMBER,Yes\n'
    'APT_RWY_END,DISPLACED_THR_LEN,"(4,0)",NUMBER,Yes\n'
    'APT_RWY_END,TDZ_ELEV,"(6,1)",NUMBER,Yes\n'
    'APT_RWY_END,VGSI_CODE,4,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_VISUAL_RANGE_EQUIP_CODE,3,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_VSBY_VALUE_EQUIP_FLAG,1,VARCHAR,Yes\n'
    'APT_RWY_END,APCH_LGT_SYSTEM_CODE,8,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_LGTS_FLAG,1,VARCHAR,Yes\n'
    'APT_RWY_END,CNTRLN_LGTS_AVBL_FLAG,1,VARCHAR,Yes\n'
    'APT_RWY_END,TDZ_LGT_AVBL_FLAG,1,VARCHAR,Yes\n'
    'APT_RWY_END,OBSTN_TYPE,11,VARCHAR,Yes\n'
    'APT_RWY_END,OBSTN_MRKD_CODE,2,VARCHAR,Yes\n'
    'APT_RWY_END,FAR_PART_77_CODE,5,VARCHAR,Yes\n'
    'APT_RWY_END,OBSTN_CLNC_SLOPE,"(2,0)",NUMBER,Yes\n'
    'APT_RWY_END,OBSTN_HGT,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,DIST_FROM_THR,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,CNTRLN_OFFSET,"(4,0)",NUMBER,Yes\n'
    'APT_RWY_END,CNTRLN_DIR_CODE,3,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_GRAD,"(4,3)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_GRAD_DIRECTION,4,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_PSN_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_PSN_DATE,10,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_ELEV_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_ELEV_DATE,10,VARCHAR,Yes\n'
    'APT_RWY_END,DSPL_THR_PSN_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_DSPL_THR_PSN_DATE,10,VARCHAR,Yes\n'
    'APT_RWY_END,DSPL_THR_ELEV_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_DSPL_THR_ELEV_DATE,10,VARCHAR,Yes\n'
    'APT_RWY_END,TDZ_ELEV_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_TDZ_ELEV_DATE,10,VARCHAR,Yes\n'
    'APT_RWY_END,TKOF_RUN_AVBL,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,TKOF_DIST_AVBL,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,ACLT_STOP_DIST_AVBL,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,LNDG_DIST_AVBL,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,LAHSO_ALD,"(5,0)",NUMBER,Yes\n'
    'APT_RWY_END,RWY_END_INTERSECT_LAHSO,7,VARCHAR,Yes\n'
    'APT_RWY_END,LAHSO_DESC,40,VARCHAR,Yes\n'
    'APT_RWY_END,LAHSO_LAT,14,VARCHAR,Yes\n'
    'APT_RWY_END,LAT_LAHSO_DECIMAL,"(10,8)",NUMBER,Yes\n'
    'APT_RWY_END,LAHSO_LONG,15,VARCHAR,Yes\n'
    'APT_RWY_END,LONG_LAHSO_DECIMAL,"(11,8)",NUMBER,Yes\n'
    'APT_RWY_END,LAHSO_PSN_SOURCE,16,VARCHAR,Yes\n'
    'APT_RWY_END,RWY_END_LAHSO_PSN_DATE,10,VARCHAR,Yes\n'
)

APT_TXT = [
    'APT03325.1*A  AIRPORT      TMB 10/29/2026       FL                                                                                   MIAMI EXEC                                                                                                                                                                                                                                                                                                                                                                                                           092331.2294N               289559.6086W                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         KTMB                                                                                                                                                                                                                                                                                                                             ',
    'RWY03325.1*A  FL09L/27R 5003 150ASPH-F      GRVD PCN/32  /F/A/Y/TMED  09L087          NNPI  F25-39-09.4872N 092349.4872N080-26-24.3015W289584.3015W    7.8 533.00                                                                     8.2P4L                                D    50                 27R267          YNPI  F25-39-11.8051N 092351.8051N080-25-29.6936W289529.6936W    7.8 403.00                                                                     8.1P2L                 TOWER      LM  D    50   85 4453100R   NGS             02/07/2010  87.0 135.0 195.0               NGS             02/07/2010NGS             02/07/2010                                                    AVN             04/11/2011                                                                                                                                                                 NGS             02/07/2010NGS             02/07/2010                                                    AVN             04/11/2011                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         ',
    'RWY03325.1*A  FL09R/27L 6000 150ASPH-F      GRVD PCN/32  /F/A/Y/THIGH 09R087ILS       YPIR  F25-38-34.4430N 092314.4430N080-26-27.9430W289587.9430W   10.0 553.00                                                                    10.0P4R      MALSR                     PIR  50                 27L267          NPIR  F25-38-37.2256N 092317.2256N080-25-22.4640W289522.4640W    7.9 533.00                                                                     8.1P4L                                D    50                 FAA             11/07/2014  87.0 135.0 195.0               FAA             02/29/2012FAA             02/28/2012                                                    FAA             02/28/2012                                                                                                                                                                 NGS             02/07/2010NGS             02/07/2010                                                    AVN             04/11/2011                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         ',
    'RWY03325.1*A  FL13/31   4001 150ASPH-E      GRVD PCN/32  /F/A/Y/TMED  13 127          NBSC  G25-39-02.0539N 092342.0539N080-26-26.8558W289586.8558W    8.0 403.00                                                                     8.1P4L              Y                 B(V) 50                 31 307          NBSC  G25-38-38.0122N 092318.0122N080-25-52.1011W289552.1011W    7.8                                                                            8.1                    POLE           B(V) 36   29 1215250L   NGS             02/07/2010  87.0 135.0 195.0               NGS             02/07/2010NGS             02/07/2010                                                    AVN             04/11/2011                                                                                                                                                                 NGS             02/07/2010NGS             02/07/2010                                                    AVN             04/11/2011                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         ',
    'RWY03325.1*A  FLH1        75  75ASPH-F      NONE                      H1                    G25-39-04.8160N 092344.8160N080-25-12.5620W289512.5620W    8.6                                                                                                                  A(V) 20                                                                                                                                                                                                                                                                                                          OWNER           07/15/2016OWNER           07/15/2016                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          ',
    'RMK03325.1*A  FLA110-1         ARPT CLSD TO NON-ENG ACFT.',
    'RMK03325.1*A  FLA110-17        PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 9R',
    'APT16723.03*A AIRPORT      POB 10/29/2026       NC                                                                                   POPE AAF                                                                                                                                                                                                                                                                                                                                                                                                             126615.2010N               284452.1880W                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         KPOB                                                                                                                                                                                                                                                                                                                             ',
    'RWY16723.03*A NC05/23   7501 150CONC-G      GRVD PCN/93  /R/B/W/THIGH 05 041          NPIR  G35-09-47.4000N 126587.4000N079-01-22.0900W284482.0900W  217.6 543.00                                                                   217.6P4L  T  N        YNN                                       23 221ILS       YPIR  G35-10-43.0000N 126643.0000N079-00-22.2800W284422.2800W  190.7 543.00                                                                   200.0P4L  T  NALSF1   NNN                                       MILITARY        07/10/2003                                 MILITARY        03/22/2013MILITARY        03/22/2013                                                    MILITARY        03/22/2013                7501                                                                                                                                             MILITARY        03/22/2013MILITARY        03/22/2013                                                    MILITARY        10/09/2014                7501                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     ',
    'RWY16723.03*A NC051/231 3000  60ASPH        NONE PCN/44  /F/A/W/TLOW  051             YNPI  G                                                                                                                                                                                                       231             NNPI  G                                                                                                                                                                                                       MILITARY        07/10/2003                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               ',
    'RMK16723.03*A NCA110-23        CAUTION: ALL TWY EDGE LGT AND RWY 05/23 EDGE AND APCH LGT EQUIPPED WITH NSTD LED FIXTURES. LED FIXTURES MAY NOT BE COMPATIBLE WITH ALL NVD OR ENHANCED FLIGHT VISION SYSTEMS.',
    'APT16476.*H   HELIPORT     7NY910/29/2026       NY                                                                                   YORKTOWN HELIPAD                                                                                                                                                                                                                                                                                                                                                                                                     148363.0000N               265692.0000W                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          ',
    'RWY16476.*H   NYH1       100 100TURF        NONE                 HIGH H1                     41-12-43.0000N 148363.0000N073-48-12.0000W265692.0000W  499.0                                                                                                                                                                                                                                                                                                                                                                        OWNER           10/01/2013                                 FAA             04/22/2025FAA             04/22/2025                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          ',
    'RMK16476.*H   NYA110-1         TWO APCH/DEP PATHS; ONE NORTH & ONE WEST, EACH DIRECTION LIGHTED BT FIVE (5) AMBER LED LIGHTS.',
    'APT18717.*A   AIRPORT      ILN 10/29/2026       OH                                                                                   WILMINGTON AIR PARK                                                                                                                                                                                                                                                                                                                                                                                                  141940.5000N               301651.6000W                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         KILN                                                                                                                                                                                                                                                                                                                             ',
    'RWY18717.*A   OH04L/22R10701 150CONC-F      GRVD PCR/920 /R/C/W/THIGH 04L037ILS       NPIR  G39-25-09.6841N 141909.6841N083-48-33.3907W301713.3907W 1053.0 623.00                                                                  1056.6P4L  TMR MALSR    Y                PIR  50                 22R217ILS       NPIR  G39-26-33.7951N 141993.7951N083-47-10.7292W301630.7292W 1076.7 713.00                                                                  1076.7P4L  TMR ALSF2    YY               PIR  50                 3RD PARTY SURVEY05/21/2007 100.0 200.0 390.0 750.0         3RD PARTY SURVEY05/21/20073RD PARTY SURVEY05/21/2007                                                    3RD PARTY SURVEY05/21/200710701107011070110701                                                                                                                                             3RD PARTY SURVEY05/21/20073RD PARTY SURVEY05/21/2007                                                    3RD PARTY SURVEY05/21/200710701107011070110701                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     ',
    'RWY18717.*A   OH04R/22L 9000 150CONC-F      GRVD PCR/900 /R/D/W/THIGH 04R037ILS/DME    PIR  G39-24-51.7868N 141891.7868N083-47-42.0864W301662.0864W 1042.5 713.00                                                                  1045.5P4L  TMR          Y                PIR  50                 22L217ILS/DME    PIR  G39-26-02.5263N 141962.5263N083-46-32.5591W301592.5591W 1076.5 713.00                                                                  1076.5P4L  TMR ALSF2    YY               PIR  50                 3RD PARTY SURVEY05/21/2007 100.0 200.0 390.0 750.0         3RD PARTY SURVEY05/21/20073RD PARTY SURVEY05/21/2007                                                    3RD PARTY SURVEY05/21/2007 9000 9000 9000 9000                                                                                                                                             3RD PARTY SURVEY05/21/20073RD PARTY SURVEY05/21/2007                                                    3RD PARTY SURVEY05/21/2007 9000 9000 9000 9000                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     ',
]

ILS_TXT = [
    'ILS103325.1*A  09RILS       I-TMB 10/29/2026MIAMI EXEC                                        MIAMI                                   FLFLORIDA             ASOTMB  6000 150I        FEDERAL AVIATION ADMIN                            FEDERAL AVIATION ADMIN                            093.3106W',
    'ILS203325.1*A  09RILS       OPERATIONAL IFR       10/02/198025-38-38.040N 92318.040N 080-25-02.985W289502.985WT                     8108.70                                    NV',
    'ILS116723.03*A 23 ILS       I-POB 10/29/2026POPE AAF                                          FAYETTEVILLE                            NCNORTH CAROLINA      ASOPOB  7501 150I        U.S. AIR FORCE                                    U.S. AIR FORCE                                    228.0007W',
    'ILS216723.03*A 23 ILS       OPERATIONAL IFR       01/01/190135-09-32.200N 126572.200N079-01-38.440W284498.440WK                 234.4110.90                                    NV',
    'ILS118717.*A   04LILS       I-HWM 10/29/2026WILMINGTON AIR PARK                               WILMINGTON                              OHOHIO                AGLILN 10701 150I        CLINTON COUNTY PORT AUTHORITY                     CLINTON COUNTY PORT AUTHORITY                     044.3107W',
    'ILS218717.*A   04LILS       OPERATIONAL IFR       01/27/202639-26-42.180N 142002.180N083-47-02.520W301622.520WF                1076.1110.70                                    NV',
    'ILS118717.*A   04RILS/DME   I-IYV 10/29/2026WILMINGTON AIR PARK                               WILMINGTON                              OHOHIO                AGLILN  9000 150I        CLINTON COUNTY PORT AUTHORITY                     CLINTON COUNTY PORT AUTHORITY                     044.3107W',
    'ILS218717.*A   04RILS/DME   DECOMMISSIONED        03/18/202439-26-10.605N 141970.605N083-46-24.616W301584.616WT                1079.6109.35                                    NV',
    'ILS118717.*A   22LILS/DME   I-RHX 10/29/2026WILMINGTON AIR PARK                               WILMINGTON                              OHOHIO                AGLILN  9000 150II       CLINTON COUNTY PORT AUTHORITY                     CLINTON COUNTY PORT AUTHORITY                     224.3207W',
    'ILS218717.*A   22LILS/DME   DECOMMISSIONED        03/18/202439-24-43.787N 141883.787N083-47-49.947W301669.947WT                  1033109.35                                    NV',
    'ILS118717.*A   22RILS       I-ILN 10/29/2026WILMINGTON AIR PARK                               WILMINGTON                              OHOHIO                AGLILN 10701 150III      CLINTON COUNTY PORT AUTHORITY                     CLINTON COUNTY PORT AUTHORITY                     224.3207W',
    'ILS218717.*A   22RILS       OPERATIONAL IFR       03/18/198239-25-00.840N 141900.840N083-48-42.080W301722.080WT                1053.1110.70                                    NV',
]

APT_RF = [
    'L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER',
    'L AN 0004 00028  E7      LOCATION IDENTIFIER',
    'L AN 0010 00032  N/A     INFORMATION EFFECTIVE DATE (MM/DD/YYYY)',
    'L AN 0002 00049  A4      ASSOCIATED STATE POST OFFICE CODE',
    'L AN 0050 00134  A2      OFFICIAL FACILITY NAME',
    'L AN 0012 00539  A19S    AIRPORT REFERENCE POINT LATITUDE (SECONDS)',
    'L AN 0012 00566  A20S    AIRPORT REFERENCE POINT LONGITUDE (SECONDS)',
    'L AN 0007 01211  NONE    ICAO IDENTIFIER',
    'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER',
    'L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER',
    'L AN 0007 00017  A30     RUNWAY IDENTIFICATION',
    'R AN 0005 00024  A31     PHYSICAL RUNWAY LENGTH (NEAREST FOOT)',
    'L AN 0016 00050  A39     PAVEMENT CLASSIFICATION',
    'L AN 0003 00071  A30A    BASE END IDENTIFIER',
    'L AN 0003 00239  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)',
    'L AN 0008 00243  A49     APPROACH LIGHT SYSTEM',
    'L AN 0003 00293  A30A    RECIPROCAL END IDENTIFIER',
    'L AN 0003 00461  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)',
    'L AN 0008 00465  A49     APPROACH LIGHT SYSTEM',
    'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER',
    'L AN 0007 00017  A30     RUNWAY IDENTIFICATION',
    'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER',
    'L AN 0015 00017  N/A     REMARK ELEMENT NAME',
    'L AN 1500 00032  N/A     REMARK TEXT',
]
# ---- end of data ---------------------------------------------------------------


def write(dest: Path, csv_tables: bool = True, txt: bool = True) -> Path:
    """Lay the fixture out the way fetch_nasr.py lays out data/nasr/."""
    if csv_tables:
        (dest / "CSV").mkdir(parents=True, exist_ok=True)
        for name, text in CSV.items():
            (dest / "CSV" / name).write_text(text, encoding="latin-1", newline="")
        (dest / "CSV" / "APT_CSV_DATA_STRUCTURE.csv").write_text(APT_CSV_DATA_STRUCTURE, encoding="latin-1", newline="")
    if txt:
        (dest / "Layout_Data").mkdir(parents=True, exist_ok=True)
        (dest / "APT.txt").write_text("\n".join(APT_TXT) + "\n", encoding="latin-1")
        (dest / "ILS.txt").write_text("\n".join(ILS_TXT) + "\n", encoding="latin-1")
        (dest / "Layout_Data" / "apt_rf.txt").write_text("\n".join(APT_RF) + "\n", encoding="latin-1")
    return dest


_LAYOUT_NAMES = (
    "ICAO IDENTIFIER", "LANDING FACILITY SITE NUMBER", "RUNWAY IDENTIFICATION", "PHYSICAL RUNWAY LENGTH",
    "PAVEMENT CLASSIFICATION", "BASE END IDENTIFIER", "RECIPROCAL END IDENTIFIER", "RUNWAY VISUAL RANGE EQUIPMENT",
    "APPROACH LIGHT SYSTEM", "LOCATION IDENTIFIER", "INFORMATION EFFECTIVE DATE", "ASSOCIATED STATE POST OFFICE CODE",
    "OFFICIAL FACILITY NAME", "AIRPORT REFERENCE POINT LATITUDE (SECONDS)",
    "AIRPORT REFERENCE POINT LONGITUDE (SECONDS)", "REMARK ELEMENT NAME", "REMARK TEXT")


def _cut(cyc: Path) -> str:
    """The data block above, cut from one extracted cycle."""
    def table(name):
        with open(cyc / "CSV" / name, encoding="latin-1", newline="") as f:
            rows = list(csv.reader(f))
        return rows[0], rows[1:]

    def dump(head, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(head)
        w.writerows(rows)
        return buf.getvalue()

    head, base = table("APT_BASE.csv")
    keep = [r for r in base if r[head.index("ARPT_ID")] in FACILITIES]
    sites = {(r[head.index("SITE_NO")], r[head.index("SITE_TYPE_CODE")]) for r in keep}
    assert len(sites) == len(FACILITIES), sites
    txt_sites = {f"{s}*{t}" for s, t in sites}
    tables = {"APT_BASE.csv": dump(head, keep)}
    remarks = set()
    for name in ("APT_RWY.csv", "APT_RWY_END.csv", "APT_RMK.csv", "ILS_BASE.csv"):
        h, rows = table(name)
        a, b = h.index("SITE_NO"), h.index("SITE_TYPE_CODE")
        rows = [r for r in rows if (r[a], r[b]) in sites]
        if name == "APT_RMK.csv":
            t = h.index("REMARK")
            rows = [r for r in rows if re.search(r"\bLED\b", r[t])] + \
                   [r for r in rows if not re.search(r"L\.?E\.?D", r[t], re.I)][:1]
            remarks = {(f"{r[a]}*{r[b]}", r[t].strip()) for r in rows}
        tables[name] = dump(h, rows)
    # An APT record also carries the names, addresses and telephone numbers of
    # the facility's owner and manager. Public FAA data, and no business of a
    # test fixture in a public repo: keep the fields the reader uses, blank the rest.
    import build_chase as bc
    lay = bc.apt_layout(cyc / "Layout_Data" / "apt_rf.txt")
    used = [(0, 27)] + [lay[k] for k in ("lid", "eff", "state", "name", "lat_s", "lon_s", "icao")]

    def only_used(line):
        out = [" "] * len(line)
        for a, b in used:
            out[a:b] = line[a:b]
        return "".join(out)

    apt, ils, rf = [], [], []
    for line in open(cyc / "APT.txt", encoding="latin-1"):
        if line[3:14].strip() not in txt_sites:
            continue
        if line[:3] == "APT":
            apt.append(only_used(line.rstrip("\n")))
        elif line[:3] == "RWY":
            apt.append(line.rstrip("\n"))
        elif line[:3] == "RMK" and (line[3:14].strip(), line[31:1531].strip()) in remarks:
            apt.append(line.rstrip())
    for line in open(cyc / "ILS.txt", encoding="latin-1"):
        if line[4:15].strip() in txt_sites and line[:4] in ("ILS1", "ILS2"):
            ils.append(line.rstrip())
    pat = re.compile(r"^\s*[LR]\s+[A-Z]{1,2}\s+(\d+)\s+(\d+)\s+\S+\s+(.+?)\s*$")
    for line in open(cyc / "Layout_Data" / "apt_rf.txt", encoding="latin-1"):
        m = pat.match(line)
        if m and m.group(3).upper().startswith(_LAYOUT_NAMES):
            rf.append(line.rstrip())
    doc = [x for x in (cyc / "CSV" / "APT_CSV_DATA_STRUCTURE.csv").read_text(encoding="latin-1").splitlines()
           if x.startswith(("CSV File", "APT_RWY_END,"))]
    nl = "\n"
    out = [f"CYCLE = {keep[0][head.index('EFF_DATE')].replace('/', '-')!r}", "", "CSV = {"]
    for name, text in tables.items():
        out += [f"    {name!r}: ("] + [f"        {x + nl!r}" for x in text.splitlines()] + ["    ),"]
    out += ["}", "", "APT_CSV_DATA_STRUCTURE = ("] + [f"    {x + nl!r}" for x in doc] + [")", ""]
    for name, lines in (("APT_TXT", apt), ("ILS_TXT", ils), ("APT_RF", rf)):
        out += [f"{name} = ["] + [f"    {x!r}," for x in lines] + ["]", ""]
    return nl.join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Run bare, as every pipeline/test_*.py is when the tests are run in a
        # loop: there is nothing to cut from. It used to die here with an
        # IndexError, which read as a failing test. Say what the file holds,
        # check the data block is whole, and rewrite nothing.
        for _name, _text in CSV.items():
            assert _text.count("\n") >= 2, f"{_name}: no data rows"
        assert APT_CSV_DATA_STRUCTURE and APT_TXT and ILS_TXT and APT_RF
        print(f"fixture data only (cycle {CYCLE}): {len(CSV)} CSV tables, {len(APT_TXT)} APT.txt lines, "
              f"{len(ILS_TXT)} ILS.txt lines. The tests that read it are in test_nasr_csv.py.")
        print("passed")
        sys.exit(0)
    me = Path(__file__)
    src = me.read_text()
    a = src.index("# ---- data: generated")
    a = src.index("\n", a) + 1
    b = src.index("# ---- end of data")
    me.write_text(src[:a] + _cut(Path(sys.argv[1])) + src[b:])
    print(f"rewrote the data block of {me.name} from {sys.argv[1]}")
