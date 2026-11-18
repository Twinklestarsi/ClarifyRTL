`timescale 1ns / 1ps
`default_nettype none

// Standalone Vortex-style native-memory to AXI4 adapter.  It preserves the
// independent AXI address/data handshakes, transaction tags, response errors,
// and elastic backpressure behavior without package or helper dependencies.
module VX_axi_adapter #(
    parameter integer DATAW      = 32,
    parameter integer IN_ADDRW   = 30,
    parameter integer AXI_ADDRW  = 32,
    parameter integer TAGW       = 8,
    parameter integer AXIIDW     = 4,
    parameter integer STRBW      = DATAW / 8,
    parameter integer SIZE_LOG2  = $clog2(STRBW),
    parameter integer PERF_W     = 32,
    parameter integer TIMING_BUG = 0
) (
    input  wire                  clk,
    input  wire                  reset,

    // Native Vortex request: req_addr is word-addressable.
    input  wire                  req_valid,
    output logic                 req_ready,
    input  wire                  req_write,
    input  wire [IN_ADDRW-1:0]   req_addr,
    input  wire [DATAW-1:0]      req_wdata,
    input  wire [STRBW-1:0]      req_strb,
    input  wire [TAGW-1:0]       req_tag,

    // Native tagged response.  Both reads and writes produce a completion.
    output logic                 rsp_valid,
    input  wire                  rsp_ready,
    output logic [DATAW-1:0]     rsp_rdata,
    output logic [TAGW-1:0]      rsp_tag,
    output logic                 rsp_error,

    // AXI4 write address channel.
    output logic                 awvalid,
    input  wire                  awready,
    output logic [AXI_ADDRW-1:0] awaddr,
    output logic [AXIIDW-1:0]    awid,
    output logic [7:0]           awlen,
    output logic [2:0]           awsize,
    output logic [1:0]           awburst,
    output logic [3:0]           awcache,
    output logic [2:0]           awprot,

    // AXI4 write data channel.
    output logic                 wvalid,
    input  wire                  wready,
    output logic [DATAW-1:0]     wdata,
    output logic [STRBW-1:0]     wstrb,
    output logic                 wlast,

    // AXI4 write response channel.
    input  wire                  bvalid,
    output logic                 bready,
    input  wire [AXIIDW-1:0]     bid,
    input  wire [1:0]            bresp,

    // AXI4 read address channel.
    output logic                 arvalid,
    input  wire                  arready,
    output logic [AXI_ADDRW-1:0] araddr,
    output logic [AXIIDW-1:0]    arid,
    output logic [7:0]           arlen,
    output logic [2:0]           arsize,
    output logic [1:0]           arburst,
    output logic [3:0]           arcache,
    output logic [2:0]           arprot,

    // AXI4 read response channel.
    input  wire                  rvalid,
    output logic                 rready,
    input  wire [DATAW-1:0]      rdata,
    input  wire [AXIIDW-1:0]     rid,
    input  wire [1:0]            rresp,
    input  wire                  rlast,

    output logic [PERF_W-1:0]    perf_reads,
    output logic [PERF_W-1:0]    perf_writes,
    output logic [PERF_W-1:0]    perf_req_stalls,
    output logic [PERF_W-1:0]    perf_rsp_stalls,
    output logic [PERF_W-1:0]    perf_axi_errors
);

    typedef enum logic [2:0] {
        ST_IDLE,
        ST_WRITE_SEND,
        ST_WRITE_RESP,
        ST_READ_SEND,
        ST_READ_RESP
    } state_t;

    state_t state_r;

    // The transaction register isolates the native request from every AXI
    // channel.  Its payload remains stable throughout arbitrary AXI stalls.
    logic [AXI_ADDRW-1:0] txn_byte_addr_r;
    logic [DATAW-1:0]     txn_wdata_r;
    logic [STRBW-1:0]     txn_strb_r;
    logic [TAGW-1:0]      txn_tag_r;
    logic [AXIIDW-1:0]    txn_id_r;

    // Write address and write data are acknowledged independently.  One side
    // may finish many cycles before the other without being re-issued.
    logic aw_done_r;
    logic w_done_r;
    logic aw_fire;
    logic w_fire;
    logic ar_fire;
    logic b_fire;
    logic r_fire;
    logic req_fire;
    logic rsp_fire;

    // One response buffer lets AXI complete while the native consumer stalls.
    // It may be replaced on the cycle its existing entry is consumed.
    logic                 rsp_valid_r;
    logic                 rsp_reveal_r;
    logic [DATAW-1:0]     rsp_data_r;
    logic [TAGW-1:0]      rsp_tag_r;
    logic                 rsp_error_r;
    logic                 rsp_slot_ready;

    logic [AXI_ADDRW-1:0] req_byte_addr;
    logic                 b_id_error;
    logic                 r_id_error;
    logic                 b_protocol_error;
    logic                 r_protocol_error;
    logic                 response_visible;

    initial begin
        if (DATAW < 8 || (DATAW % 8) != 0)
            $fatal(1, "DATAW must be byte divisible");
        if ((1 << SIZE_LOG2) != STRBW)
            $fatal(1, "AXI data bytes must be a power of two");
        if (AXI_ADDRW < (IN_ADDRW + SIZE_LOG2))
            $fatal(1, "AXI address width cannot hold shifted native address");
        if (TAGW < AXIIDW)
            $fatal(1, "TAGW must be at least AXIIDW");
    end

    // Convert Vortex word addressing to AXI byte addressing.
    always_comb begin
        req_byte_addr = AXI_ADDRW'(req_addr) << SIZE_LOG2;
    end

    // Native request acceptance is decoupled from AXI ready.  The adapter can
    // also accept another request while an older native response is stalled;
    // AXI completion then waits safely at bready/rready.
    always_comb begin
        req_ready = (state_r == ST_IDLE);
        req_fire  = req_valid && req_ready;
    end

    // AXI request payload and standard single-beat control values.
    always_comb begin
        awvalid = (state_r == ST_WRITE_SEND) && !aw_done_r;
        awaddr  = txn_byte_addr_r;
        awid    = txn_id_r;
        awlen   = 8'd0;
        awsize  = 3'(SIZE_LOG2);
        awburst = 2'b01;
        awcache = 4'b0011;
        awprot  = 3'b000;

        wvalid = (state_r == ST_WRITE_SEND) && !w_done_r;
        wdata  = txn_wdata_r;
        wstrb  = txn_strb_r;
        wlast  = 1'b1;

        arvalid = (state_r == ST_READ_SEND);
        araddr  = txn_byte_addr_r;
        arid    = txn_id_r;
        arlen   = 8'd0;
        arsize  = 3'(SIZE_LOG2);
        arburst = 2'b01;
        arcache = 4'b0011;
        arprot  = 3'b000;
    end

    always_comb begin
        aw_fire = awvalid && awready;
        w_fire  = wvalid && wready;
        ar_fire = arvalid && arready;
    end

    // AXI response backpressure follows the elastic native response slot.
    // Responses with protocol errors are still accepted and reported.
    always_comb begin
        // MUTATION POINT: TIMING_BUG delays B/R completion visibility by one clock.
        response_visible = rsp_valid_r
                         && ((TIMING_BUG == 0) || rsp_reveal_r);
        rsp_valid = response_visible;
        rsp_rdata = rsp_data_r;
        rsp_tag   = rsp_tag_r;
        rsp_error = rsp_error_r;
        rsp_fire  = rsp_valid && rsp_ready;

        rsp_slot_ready = !rsp_valid_r || rsp_fire;
        bready = (state_r == ST_WRITE_RESP) && rsp_slot_ready;
        rready = (state_r == ST_READ_RESP) && rsp_slot_ready;
        b_fire = bvalid && bready;
        r_fire = rvalid && rready;

        b_id_error       = (bid != txn_id_r);
        r_id_error       = (rid != txn_id_r);
        b_protocol_error = (bresp != 2'b00) || b_id_error;
        r_protocol_error = (rresp != 2'b00) || r_id_error || !rlast;
    end

    // Request/AXI transaction state.  The two write-channel done bits provide
    // the key behavior missing from adapters that require simultaneous ready.
    always_ff @(posedge clk) begin
        if (reset) begin
            state_r         <= ST_IDLE;
            txn_byte_addr_r <= '0;
            txn_wdata_r     <= '0;
            txn_strb_r      <= '0;
            txn_tag_r       <= '0;
            txn_id_r        <= '0;
            aw_done_r       <= 1'b0;
            w_done_r        <= 1'b0;
        end else begin
            case (state_r)
                ST_IDLE: begin
                    aw_done_r <= 1'b0;
                    w_done_r  <= 1'b0;
                    if (req_fire) begin
                        txn_byte_addr_r <= req_byte_addr;
                        txn_wdata_r     <= req_wdata;
                        txn_strb_r      <= req_strb;
                        txn_tag_r       <= req_tag;
                        txn_id_r        <= AXIIDW'(req_tag);
                        if (req_write)
                            state_r <= ST_WRITE_SEND;
                        else
                            state_r <= ST_READ_SEND;
                    end
                end

                ST_WRITE_SEND: begin
                    if (aw_fire)
                        aw_done_r <= 1'b1;
                    if (w_fire)
                        w_done_r <= 1'b1;
                    if ((aw_done_r || aw_fire)
                     && (w_done_r || w_fire))
                        state_r <= ST_WRITE_RESP;
                end

                ST_WRITE_RESP: begin
                    if (b_fire)
                        state_r <= ST_IDLE;
                end

                ST_READ_SEND: begin
                    if (ar_fire)
                        state_r <= ST_READ_RESP;
                end

                ST_READ_RESP: begin
                    if (r_fire)
                        state_r <= ST_IDLE;
                end

                default: begin
                    state_r <= ST_IDLE;
                end
            endcase
        end
    end

    // Capture both read and write completions in the same native response
    // format.  A write returns zero data; errors include AXI status and ID.
    always_ff @(posedge clk) begin
        if (reset) begin
            rsp_valid_r  <= 1'b0;
            rsp_reveal_r <= 1'b0;
            rsp_data_r   <= '0;
            rsp_tag_r    <= '0;
            rsp_error_r  <= 1'b0;
        end else begin
            if (rsp_valid_r)
                rsp_reveal_r <= 1'b1;

            if (rsp_fire) begin
                rsp_valid_r  <= 1'b0;
                rsp_reveal_r <= 1'b0;
            end

            if (b_fire) begin
                rsp_valid_r  <= 1'b1;
                rsp_reveal_r <= 1'b0;
                rsp_data_r   <= '0;
                rsp_tag_r    <= txn_tag_r;
                rsp_error_r  <= b_protocol_error;
            end else if (r_fire) begin
                rsp_valid_r  <= 1'b1;
                rsp_reveal_r <= 1'b0;
                rsp_data_r   <= rdata;
                rsp_tag_r    <= txn_tag_r;
                rsp_error_r  <= r_protocol_error;
            end
        end
    end

    // Event counters make request stalls, response stalls, and malformed AXI
    // completions observable to both software and the verification model.
    always_ff @(posedge clk) begin
        if (reset) begin
            perf_reads      <= '0;
            perf_writes     <= '0;
            perf_req_stalls <= '0;
            perf_rsp_stalls <= '0;
            perf_axi_errors <= '0;
        end else begin
            if (req_fire && req_write)
                perf_writes <= perf_writes + PERF_W'(1);
            if (req_fire && !req_write)
                perf_reads <= perf_reads + PERF_W'(1);
            if (req_valid && !req_ready)
                perf_req_stalls <= perf_req_stalls + PERF_W'(1);
            if (rsp_valid && !rsp_ready)
                perf_rsp_stalls <= perf_rsp_stalls + PERF_W'(1);
            if ((b_fire && b_protocol_error)
             || (r_fire && r_protocol_error))
                perf_axi_errors <= perf_axi_errors + PERF_W'(1);
        end
    end

    // Payload stability checks guard the independent-channel contract.  They
    // catch accidental changes to an address or data beat during backpressure.
    logic [AXI_ADDRW-1:0] stalled_awaddr_r;
    logic [DATAW-1:0]     stalled_wdata_r;
    logic [STRBW-1:0]     stalled_wstrb_r;
    logic [AXI_ADDRW-1:0] stalled_araddr_r;
    logic                 aw_stalled_r;
    logic                 w_stalled_r;
    logic                 ar_stalled_r;

    always_ff @(posedge clk) begin
        if (reset) begin
            aw_stalled_r <= 1'b0;
            w_stalled_r  <= 1'b0;
            ar_stalled_r <= 1'b0;
        end else begin
            if (aw_stalled_r && (awaddr != stalled_awaddr_r))
                $error("AW payload changed while stalled");
            if (w_stalled_r
             && ((wdata != stalled_wdata_r) || (wstrb != stalled_wstrb_r)))
                $error("W payload changed while stalled");
            if (ar_stalled_r && (araddr != stalled_araddr_r))
                $error("AR payload changed while stalled");

            aw_stalled_r <= awvalid && !awready;
            w_stalled_r  <= wvalid && !wready;
            ar_stalled_r <= arvalid && !arready;
            stalled_awaddr_r <= awaddr;
            stalled_wdata_r  <= wdata;
            stalled_wstrb_r  <= wstrb;
            stalled_araddr_r <= araddr;
        end
    end

endmodule

`default_nettype wire
