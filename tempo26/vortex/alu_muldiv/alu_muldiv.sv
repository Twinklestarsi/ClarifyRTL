`timescale 1ns / 1ps
`default_nettype none

// Standalone multi-lane implementation of the RISC-V M extension.
module VX_alu_muldiv #(
    parameter integer DATAW       = 32,
    parameter integer NUM_LANES   = 4,
    parameter integer TAGW        = 8,
    parameter integer WIDW        = 4,
    parameter integer RDW         = 5,
    parameter integer MUL_LATENCY = 3,
    parameter integer DIV_LATENCY = 8,
    parameter integer TIMING_BUG  = 0,
    parameter integer MUL_COUNTW  = $clog2(MUL_LATENCY + 2),
    parameter integer DIV_COUNTW  = $clog2(DIV_LATENCY + 2)
) (
    input  wire                         clk,
    input  wire                         reset,

    input  wire                         in_valid,
    output wire                         in_ready,
    input  wire [2:0]                   in_op,
    input  wire [NUM_LANES*DATAW-1:0]  in_a,
    input  wire [NUM_LANES*DATAW-1:0]  in_b,
    input  wire [NUM_LANES-1:0]        in_tmask,
    input  wire [TAGW-1:0]             in_tag,
    input  wire [WIDW-1:0]             in_wid,
    input  wire [RDW-1:0]              in_rd,
    input  wire                         in_wb,
    input  wire                         in_sop,
    input  wire                         in_eop,

    output wire                         out_valid,
    input  wire                         out_ready,
    output wire [NUM_LANES*DATAW-1:0]  out_result,
    output wire [NUM_LANES-1:0]        out_tmask,
    output wire [TAGW-1:0]             out_tag,
    output wire [WIDW-1:0]             out_wid,
    output wire [RDW-1:0]              out_rd,
    output wire                         out_wb,
    output wire                         out_sop,
    output wire                         out_eop
);

    localparam [2:0] OP_MUL    = 3'd0;
    localparam [2:0] OP_MULH   = 3'd1;
    localparam [2:0] OP_MULHSU = 3'd2;
    localparam [2:0] OP_MULHU  = 3'd3;
    localparam [2:0] OP_DIV    = 3'd4;
    localparam [2:0] OP_DIVU   = 3'd5;
    localparam [2:0] OP_REM    = 3'd6;
    localparam [2:0] OP_REMU   = 3'd7;

    typedef logic [NUM_LANES*DATAW-1:0] lane_data_t;

    logic                               mul_busy_r;
    logic [MUL_COUNTW-1:0]              mul_count_r;
    lane_data_t                         mul_work_data_r;
    logic [NUM_LANES-1:0]               mul_work_tmask_r;
    logic [TAGW-1:0]                    mul_work_tag_r;
    logic [WIDW-1:0]                    mul_work_wid_r;
    logic [RDW-1:0]                     mul_work_rd_r;
    logic                               mul_work_wb_r;
    logic                               mul_work_sop_r;
    logic                               mul_work_eop_r;

    logic                               div_busy_r;
    logic [DIV_COUNTW-1:0]              div_count_r;
    lane_data_t                         div_work_data_r;
    logic [NUM_LANES-1:0]               div_work_tmask_r;
    logic [TAGW-1:0]                    div_work_tag_r;
    logic [WIDW-1:0]                    div_work_wid_r;
    logic [RDW-1:0]                     div_work_rd_r;
    logic                               div_work_wb_r;
    logic                               div_work_sop_r;
    logic                               div_work_eop_r;

    logic                               mul_rsp_valid_r;
    lane_data_t                         mul_rsp_data_r;
    logic [NUM_LANES-1:0]               mul_rsp_tmask_r;
    logic [TAGW-1:0]                    mul_rsp_tag_r;
    logic [WIDW-1:0]                    mul_rsp_wid_r;
    logic [RDW-1:0]                     mul_rsp_rd_r;
    logic                               mul_rsp_wb_r;
    logic                               mul_rsp_sop_r;
    logic                               mul_rsp_eop_r;

    logic                               div_rsp_valid_r;
    lane_data_t                         div_rsp_data_r;
    logic [NUM_LANES-1:0]               div_rsp_tmask_r;
    logic [TAGW-1:0]                    div_rsp_tag_r;
    logic [WIDW-1:0]                    div_rsp_wid_r;
    logic [RDW-1:0]                     div_rsp_rd_r;
    logic                               div_rsp_wb_r;
    logic                               div_rsp_sop_r;
    logic                               div_rsp_eop_r;

    logic                               prefer_div_r;

    wire is_multiply = (in_op == OP_MUL)
                    || (in_op == OP_MULH)
                    || (in_op == OP_MULHSU)
                    || (in_op == OP_MULHU);

    wire mul_available = !mul_busy_r && !mul_rsp_valid_r;
    wire div_available = !div_busy_r && !div_rsp_valid_r;
    assign in_ready = is_multiply ? mul_available : div_available;
    wire input_fire = in_valid && in_ready;
    wire mul_accept = input_fire && is_multiply;
    wire div_accept = input_fire && !is_multiply;

    function automatic [DATAW-1:0] lane_execute;
        input [2:0] op;
        input [DATAW-1:0] lhs;
        input [DATAW-1:0] rhs;
        logic signed [DATAW-1:0] signed_lhs;
        logic signed [DATAW-1:0] signed_rhs;
        logic [2*DATAW-1:0] unsigned_product;
        logic signed [2*DATAW-1:0] signed_product;
        logic signed [DATAW:0] mixed_lhs;
        logic signed [DATAW:0] mixed_rhs;
        logic signed [2*DATAW+1:0] mixed_product;
        logic signed [DATAW-1:0] min_signed;
        begin
            signed_lhs = lhs;
            signed_rhs = rhs;
            unsigned_product = lhs * rhs;
            signed_product = signed_lhs * signed_rhs;
            mixed_lhs = {signed_lhs[DATAW-1], signed_lhs};
            mixed_rhs = {1'b0, rhs};
            mixed_product = mixed_lhs * mixed_rhs;
            min_signed = {1'b1, {DATAW-1{1'b0}}};
            case (op)
                OP_MUL: begin
                    lane_execute = unsigned_product[DATAW-1:0];
                end
                OP_MULH: begin
                    lane_execute = signed_product[2*DATAW-1:DATAW];
                end
                OP_MULHSU: begin
                    lane_execute = mixed_product[2*DATAW-1:DATAW];
                end
                OP_MULHU: begin
                    lane_execute = unsigned_product[2*DATAW-1:DATAW];
                end
                OP_DIV: begin
                    if (rhs == '0)
                        lane_execute = {DATAW{1'b1}};
                    else if ((signed_lhs == min_signed) && (signed_rhs == -1))
                        lane_execute = lhs;
                    else
                        lane_execute = signed_lhs / signed_rhs;
                end
                OP_DIVU: begin
                    if (rhs == '0)
                        lane_execute = {DATAW{1'b1}};
                    else
                        lane_execute = lhs / rhs;
                end
                OP_REM: begin
                    if (rhs == '0)
                        lane_execute = lhs;
                    else if ((signed_lhs == min_signed) && (signed_rhs == -1))
                        lane_execute = '0;
                    else
                        lane_execute = signed_lhs % signed_rhs;
                end
                OP_REMU: begin
                    if (rhs == '0)
                        lane_execute = lhs;
                    else
                        lane_execute = lhs % rhs;
                end
                default: begin
                    lane_execute = '0;
                end
            endcase
        end
    endfunction

    function automatic lane_data_t vector_execute;
        input [2:0] op;
        input lane_data_t lhs_vector;
        input lane_data_t rhs_vector;
        integer lane;
        begin
            vector_execute = '0;
            for (lane = 0; lane < NUM_LANES; lane = lane + 1) begin
                vector_execute[lane*DATAW +: DATAW] = lane_execute(
                    op,
                    lhs_vector[lane*DATAW +: DATAW],
                    rhs_vector[lane*DATAW +: DATAW]
                );
            end
        end
    endfunction

    wire choose_div = div_rsp_valid_r
                   && (!mul_rsp_valid_r || prefer_div_r);
    wire choose_mul = mul_rsp_valid_r
                   && (!div_rsp_valid_r || !prefer_div_r);
    wire mul_rsp_fire = choose_mul && out_ready;
    wire div_rsp_fire = choose_div && out_ready;

    assign out_valid  = choose_mul || choose_div;
    assign out_result = choose_div ? div_rsp_data_r  : mul_rsp_data_r;
    assign out_tmask  = choose_div ? div_rsp_tmask_r : mul_rsp_tmask_r;
    assign out_tag    = choose_div ? div_rsp_tag_r   : mul_rsp_tag_r;
    assign out_wid    = choose_div ? div_rsp_wid_r   : mul_rsp_wid_r;
    assign out_rd     = choose_div ? div_rsp_rd_r    : mul_rsp_rd_r;
    assign out_wb     = choose_div ? div_rsp_wb_r    : mul_rsp_wb_r;
    assign out_sop    = choose_div ? div_rsp_sop_r   : mul_rsp_sop_r;
    assign out_eop    = choose_div ? div_rsp_eop_r   : mul_rsp_eop_r;

    always @(posedge clk) begin
        if (reset) begin
            mul_busy_r       <= 1'b0;
            mul_count_r      <= '0;
            mul_work_data_r  <= '0;
            mul_work_tmask_r <= '0;
            mul_work_tag_r   <= '0;
            mul_work_wid_r   <= '0;
            mul_work_rd_r    <= '0;
            mul_work_wb_r    <= 1'b0;
            mul_work_sop_r   <= 1'b0;
            mul_work_eop_r   <= 1'b0;
            mul_rsp_valid_r  <= 1'b0;
            mul_rsp_data_r   <= '0;
            mul_rsp_tmask_r  <= '0;
            mul_rsp_tag_r    <= '0;
            mul_rsp_wid_r    <= '0;
            mul_rsp_rd_r     <= '0;
            mul_rsp_wb_r     <= 1'b0;
            mul_rsp_sop_r    <= 1'b0;
            mul_rsp_eop_r    <= 1'b0;
        end else begin
            if (mul_rsp_fire)
                mul_rsp_valid_r <= 1'b0;
            if (mul_accept) begin
                mul_busy_r       <= 1'b1;
                // The mutant loads one extra count and returns a cycle late.
                mul_count_r      <= MUL_COUNTW'(MUL_LATENCY - 1 + TIMING_BUG);
                mul_work_data_r  <= vector_execute(in_op, in_a, in_b);
                mul_work_tmask_r <= in_tmask;
                mul_work_tag_r   <= in_tag;
                mul_work_wid_r   <= in_wid;
                mul_work_rd_r    <= in_rd;
                mul_work_wb_r    <= in_wb;
                mul_work_sop_r   <= in_sop;
                mul_work_eop_r   <= in_eop;
            end else if (mul_busy_r) begin
                if (mul_count_r == 0) begin
                    mul_busy_r       <= 1'b0;
                    mul_rsp_valid_r  <= 1'b1;
                    mul_rsp_data_r   <= mul_work_data_r;
                    mul_rsp_tmask_r  <= mul_work_tmask_r;
                    mul_rsp_tag_r    <= mul_work_tag_r;
                    mul_rsp_wid_r    <= mul_work_wid_r;
                    mul_rsp_rd_r     <= mul_work_rd_r;
                    mul_rsp_wb_r     <= mul_work_wb_r;
                    mul_rsp_sop_r    <= mul_work_sop_r;
                    mul_rsp_eop_r    <= mul_work_eop_r;
                end else begin
                    mul_count_r <= mul_count_r - 1'b1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            div_busy_r       <= 1'b0;
            div_count_r      <= '0;
            div_work_data_r  <= '0;
            div_work_tmask_r <= '0;
            div_work_tag_r   <= '0;
            div_work_wid_r   <= '0;
            div_work_rd_r    <= '0;
            div_work_wb_r    <= 1'b0;
            div_work_sop_r   <= 1'b0;
            div_work_eop_r   <= 1'b0;
            div_rsp_valid_r  <= 1'b0;
            div_rsp_data_r   <= '0;
            div_rsp_tmask_r  <= '0;
            div_rsp_tag_r    <= '0;
            div_rsp_wid_r    <= '0;
            div_rsp_rd_r     <= '0;
            div_rsp_wb_r     <= 1'b0;
            div_rsp_sop_r    <= 1'b0;
            div_rsp_eop_r    <= 1'b0;
        end else begin
            if (div_rsp_fire)
                div_rsp_valid_r <= 1'b0;
            if (div_accept) begin
                div_busy_r       <= 1'b1;
                div_count_r      <= DIV_COUNTW'(DIV_LATENCY - 1);
                div_work_data_r  <= vector_execute(in_op, in_a, in_b);
                div_work_tmask_r <= in_tmask;
                div_work_tag_r   <= in_tag;
                div_work_wid_r   <= in_wid;
                div_work_rd_r    <= in_rd;
                div_work_wb_r    <= in_wb;
                div_work_sop_r   <= in_sop;
                div_work_eop_r   <= in_eop;
            end else if (div_busy_r) begin
                if (div_count_r == 0) begin
                    div_busy_r       <= 1'b0;
                    div_rsp_valid_r  <= 1'b1;
                    div_rsp_data_r   <= div_work_data_r;
                    div_rsp_tmask_r  <= div_work_tmask_r;
                    div_rsp_tag_r    <= div_work_tag_r;
                    div_rsp_wid_r    <= div_work_wid_r;
                    div_rsp_rd_r     <= div_work_rd_r;
                    div_rsp_wb_r     <= div_work_wb_r;
                    div_rsp_sop_r    <= div_work_sop_r;
                    div_rsp_eop_r    <= div_work_eop_r;
                end else begin
                    div_count_r <= div_count_r - 1'b1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            prefer_div_r <= 1'b0;
        end else if (mul_rsp_fire) begin
            prefer_div_r <= 1'b1;
        end else if (div_rsp_fire) begin
            prefer_div_r <= 1'b0;
        end
    end

    initial begin
        if (DATAW < 2)
            $fatal(1, "DATAW must be at least two");
        if (NUM_LANES < 1)
            $fatal(1, "NUM_LANES must be positive");
        if (MUL_LATENCY < 1 || DIV_LATENCY < 1)
            $fatal(1, "operation latencies must be positive");
    end

endmodule

`default_nettype wire
